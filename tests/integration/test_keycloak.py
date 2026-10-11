"""Opt-in real provider and network MCP test; no browser or production credentials."""

import asyncio
import base64
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
import jwt
import pytest
from cryptography.fernet import Fernet
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from thinkhome_zulip_mcp.executor import Principal
from thinkhome_zulip_mcp.store import IdentityStore

pytestmark = pytest.mark.skipif(
    not os.environ.get("KEYCLOAK_HOME"),
    reason="Set KEYCLOAK_HOME for real provider test",
)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def process(command, env, log_path):
    with log_path.open("wb") as log:
        child = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            yield child
        finally:
            child.terminate()
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)


async def ready(url, child):
    async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            assert child.poll() is None, "Fixture process exited before readiness"
            try:
                response = await client.get(url)
                if response.status_code == 200:
                    return response.json()
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
    raise AssertionError("Fixture readiness timed out")


class LoginForm(HTMLParser):
    action = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and attrs.get("id") == "kc-form-login":
            self.action = attrs.get("action")


async def login(discovery, username, password, callback, *, wrong_verifier=False):
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    state = secrets.token_urlsafe(24)
    params = dict(
        client_id="mcp-test",
        response_type="code",
        redirect_uri=callback,
        scope="openid zulip:access",
        state=state,
        code_challenge=challenge,
        code_challenge_method="S256",
    )
    async with httpx.AsyncClient(
        trust_env=False, follow_redirects=False, timeout=15
    ) as client:
        page = await client.get(discovery["authorization_endpoint"], params=params)
        assert page.status_code == 200
        form = LoginForm()
        form.feed(page.text)
        assert form.action is not None
        assert urlsplit(form.action).netloc == urlsplit(discovery["issuer"]).netloc
        # Keycloak trusts loopback secure cookies; httpx's CookieJar does not.
        assert urlsplit(form.action).hostname == "127.0.0.1"
        for cookie in client.cookies.jar:
            cookie.secure = False
        response = await client.post(
            form.action, data={"username": username, "password": password}
        )
        assert response.status_code == 302, "Keycloak login did not complete"
        redirect = response.headers["location"]
        assert redirect.startswith(callback + "?")
        query = parse_qs(urlsplit(redirect).query)
        assert query["state"] == [state]
        body = dict(
            grant_type="authorization_code",
            client_id="mcp-test",
            redirect_uri=callback,
            code=query["code"][0],
            code_verifier=secrets.token_urlsafe(48) if wrong_verifier else verifier,
        )
        response = await client.post(discovery["token_endpoint"], data=body)
        if wrong_verifier:
            assert response.status_code == 400
            return None
        assert response.status_code == 200, "Keycloak token exchange failed"
        tokens = response.json()
        replay = await client.post(discovery["token_endpoint"], data=body)
        assert replay.status_code == 400
        return tokens["access_token"]


@pytest.mark.asyncio
async def test_real_keycloak_pkce_and_network_mcp(tmp_path: Path):
    realm = json.loads(
        (Path(__file__).parents[1] / "fixtures/keycloak-realm.json").read_text()
    )
    assert realm["clients"][0]["attributes"]["pkce.code.challenge.method"] == "S256"
    assert realm["clients"][0]["directAccessGrantsEnabled"] is False
    keycloak = tmp_path / "keycloak"
    shutil.copytree(os.environ["KEYCLOAK_HOME"], keycloak)
    issuer_base = f"http://127.0.0.1:{free_port()}"
    resource_url = f"http://127.0.0.1:{free_port()}/mcp"
    callback = "http://127.0.0.1:9876/callback"
    password = secrets.token_urlsafe(32)
    realm["clients"][0]["redirectUris"] = [callback]
    realm["clientScopes"][0]["protocolMappers"][0]["config"][
        "included.custom.audience"
    ] = resource_url
    for user in realm["users"]:
        user["credentials"] = [
            {"type": "password", "value": password, "temporary": False}
        ]
    imports = keycloak / "data/import"
    imports.mkdir(parents=True, exist_ok=True)
    (imports / "mcp-test-realm.json").write_text(json.dumps(realm))
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "JAVA_HOME": os.environ["JAVA_HOME"],
        "JAVA_OPTS_APPEND": "-Xms64m -Xmx512m",
    }
    command = [
        str(keycloak / "bin/kc.sh"),
        "start-dev",
        "--import-realm",
        "--http-host=127.0.0.1",
        f"--http-port={urlsplit(issuer_base).port}",
        f"--hostname={issuer_base}",
        "--http-management-host=127.0.0.1",
        f"--http-management-port={free_port()}",
    ]
    with process(command, env, tmp_path / "keycloak.log") as provider:
        discovery = await ready(
            issuer_base + "/realms/mcp-test/.well-known/openid-configuration", provider
        )
        assert discovery["issuer"] == issuer_base + "/realms/mcp-test"
        assert "S256" in discovery["code_challenge_methods_supported"]
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            missing_pkce = await client.get(
                discovery["authorization_endpoint"],
                params={
                    "client_id": "mcp-test",
                    "response_type": "code",
                    "redirect_uri": callback,
                    "scope": "openid zulip:access",
                },
            )
            assert missing_pkce.status_code == 302
            error_redirect = missing_pkce.headers["location"]
            assert error_redirect.startswith(callback + "?")
            error = parse_qs(urlsplit(error_redirect).query)
            assert error["error"] == ["invalid_request"]
            assert "code_challenge" in error["error_description"][0]

        assert (
            await login(discovery, "alice", password, callback, wrong_verifier=True)
            is None
        )
        tokens = await asyncio.gather(
            *[login(discovery, name, password, callback) for name in ("alice", "bob")]
        )
        for token in tokens:
            claims = jwt.decode(token, options={"verify_signature": False})
            assert "sub" in claims, sorted(claims)
            assert "zulip:access" in claims.get("scope", "").split()
            assert claims["iss"] == discovery["issuer"]
            assert resource_url in (
                [claims["aud"]] if isinstance(claims["aud"], str) else claims["aud"]
            )
        key_file = tmp_path / "store-key"
        key_file.write_bytes(Fernet.generate_key())
        key_file.chmod(0o600)
        store = IdentityStore(tmp_path / "identities.db", key_file.read_bytes())
        for user in realm["users"]:
            store.add_identity(
                Principal(
                    user["username"],
                    "https://zulip.example",
                    user["email"],
                    "fixture-only",
                )
            )
            store.bind_oauth(
                user["username"], issuer=discovery["issuer"], subject=user["id"]
            )
        env.update(
            MCP_STORE_PATH=str(store.path),
            MCP_KEY_FILE=str(key_file),
            MCP_RESOURCE_URL=resource_url,
            ZULIP_URL="https://zulip.example",
            MCP_ALLOW_INSECURE_HTTP="1",
            MCP_OAUTH_ISSUER=discovery["issuer"],
            MCP_OAUTH_JWKS_URL=discovery["jwks_uri"],
        )
        with process(
            [
                sys.executable,
                "-m",
                "thinkhome_zulip_mcp",
                "serve",
                "--transport",
                "http",
                "--port",
                str(urlsplit(resource_url).port),
            ],
            env,
            tmp_path / "mcp.log",
        ) as server:
            metadata = await ready(
                resource_url.replace(
                    "/mcp", "/.well-known/oauth-protected-resource/mcp"
                ),
                server,
            )
            assert metadata["authorization_servers"] == [discovery["issuer"]]

            async def mcp_client(token):
                async with httpx2.AsyncClient(
                    headers={"authorization": f"Bearer {token}"}, trust_env=False
                ) as client:
                    async with streamable_http_client(
                        resource_url, http_client=client
                    ) as streams:
                        async with ClientSession(streams[0], streams[1]) as session:
                            await session.initialize()
                            result = await session.call_tool("zulip_capabilities", {})
                            assert not result.is_error
                            assert (
                                result.structured_content["realm_url"]
                                == "https://zulip.example"
                            )
                            assert result.structured_content["transport"] == "http"

            await asyncio.gather(*[mcp_client(token) for token in tokens])
            store.remove_identity("alice")
            async with httpx.AsyncClient(trust_env=False) as client:
                denied = await client.post(
                    resource_url,
                    headers={"authorization": f"Bearer {tokens[0]}"},
                    json={},
                )
                assert denied.status_code == 401
        for log in (tmp_path / "mcp.log", tmp_path / "keycloak.log"):
            text = log.read_text()
            assert password not in text
            assert all(token not in text for token in tokens)

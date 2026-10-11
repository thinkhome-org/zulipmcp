import asyncio
import base64
import json
import time
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa

from thinkhome_zulip_mcp.auth import AuthConfig, Authenticator
from thinkhome_zulip_mcp.executor import Executor, Principal
from thinkhome_zulip_mcp.server import create_http_app
from thinkhome_zulip_mcp.store import IdentityStore


@pytest.mark.asyncio
@pytest.mark.parametrize("oauth", [False, True])
async def test_http_identity_isolation_revoke_and_session_termination(
    tmp_path: Path, oauth, caplog
):
    store = IdentityStore(tmp_path / "db", Fernet.generate_key())
    tokens = {}
    for name in ("alice", "bob"):
        store.add_identity(
            Principal(name, "https://zulip.example", name, f"key-{name}")
        )
        tokens[name] = store.issue_token(name, expires_at=int(time.time()) + 300)

    def upstream(request):
        user = base64.b64decode(request.headers["authorization"].split()[1]).decode()
        return httpx.Response(200, json={"result": "success", "test_credentials": user})

    config = AuthConfig("https://mcp.example/mcp", "https://zulip.example")
    transport = None
    if oauth:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        jwk.update(kid="integration", alg="RS256", use="sig")
        config = AuthConfig(
            config.resource_url,
            config.realm_url,
            issuer="https://issuer.example",
            jwks_url="https://issuer.example/keys",
        )
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"keys": [jwk]})
        )
        for name in tokens:
            store.bind_oauth(name, issuer=config.issuer, subject=name)
            tokens[name] = jwt.encode(
                {
                    "iss": config.issuer,
                    "sub": name,
                    "aud": config.resource_url,
                    "scope": "zulip:access",
                    "azp": "same-client-for-both-users",
                    "iat": int(time.time()),
                    "exp": int(time.time()) + 300,
                },
                key,
                algorithm="RS256",
                headers={"kid": "integration"},
            )
    auth = Authenticator(store, config, transport=transport)
    app = create_http_app(auth, Executor(transport=httpx.MockTransport(upstream)))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://mcp.example"
        ) as client,
    ):
        response = await client.post("/mcp", json={})
        assert response.status_code == 401
        assert "resource_metadata=" in response.headers["www-authenticate"]
        metadata = (
            await client.get("/.well-known/oauth-protected-resource/mcp")
        ).json()
        assert metadata["resource"] == auth.config.resource_url
        assert metadata.get("authorization_servers", []) == (
            [config.issuer] if oauth else []
        )

        async def initialize(name):
            headers = {
                "authorization": f"Bearer {tokens[name]}",
                "accept": "application/json, text/event-stream",
            }
            response = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
            )
            assert response.status_code == 200, response.text
            headers["mcp-session-id"] = response.headers["mcp-session-id"]
            headers["mcp-protocol-version"] = response.json()["result"][
                "protocolVersion"
            ]
            await client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            return headers

        alice, bob = await asyncio.gather(initialize("alice"), initialize("bob"))
        rejected = await client.post(
            "/mcp", headers={**alice, "origin": "https://attacker.example"}, json={}
        )
        assert rejected.status_code == 403
        assert (
            await client.post("/mcp", params={"access_token": tokens["alice"]}, json={})
        ).status_code == 401
        call = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "zulip_execute",
                "arguments": {"operation_id": "get-own-user", "arguments": {}},
            },
        }
        results = await asyncio.gather(
            *[
                client.post("/mcp", headers=headers, json=call)
                for headers in (alice, bob)
            ]
        )
        for name, result in zip(("alice", "bob"), results, strict=True):
            assert result.status_code == 200, result.text
            data = result.json()["result"]["structuredContent"]["data"]
            assert data["test_credentials"] == f"{name}:key-{name}"
        hijack = {**alice, "authorization": bob["authorization"]}
        assert (await client.post("/mcp", headers=hijack, json=call)).status_code in (
            403,
            404,
        )
        if oauth:
            store.remove_identity("alice")
        else:
            store.revoke_token(tokens["alice"])
        assert (await client.post("/mcp", headers=alice, json=call)).status_code == 401
        assert (await client.delete("/mcp", headers=bob)).status_code == 200
        assert (await client.post("/mcp", headers=bob, json=call)).status_code == 404

    assert "key-alice" not in caplog.text
    assert "key-bob" not in caplog.text
    assert all(token not in caplog.text for token in tokens.values())

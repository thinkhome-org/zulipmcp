import time

import httpx
import pytest
from cryptography.fernet import Fernet

from thinkhome_zulip_mcp.auth import AuthConfig, Authenticator
from thinkhome_zulip_mcp.channel_policy import ChannelPolicy
from thinkhome_zulip_mcp.executor import Executor, Principal
from thinkhome_zulip_mcp.server import create_http_app
from thinkhome_zulip_mcp.store import IdentityStore


@pytest.mark.asyncio
async def test_user_discovery_is_caller_scoped_and_profile_rechecks_revocation(
    tmp_path,
):
    store = IdentityStore(tmp_path / "db", Fernet.generate_key())
    tokens = {}
    for name in ("alice", "bob"):
        store.add_identity(Principal(name, "https://zulip.example", name, "synthetic"))
        tokens[name] = store.issue_token(name, expires_at=int(time.time()) + 300)
    calls = []

    def upstream(request):
        calls.append(request)
        actor = next(
            name
            for name in tokens
            if request.headers["authorization"]
            == httpx.BasicAuth(name, "synthetic")._auth_header
        )
        user = {
            "user_id": 1 if actor == "alice" else 2,
            "full_name": actor,
            "email": f"{actor}@example",
        }
        if request.url.path == "/api/v1/users":
            return httpx.Response(200, json={"result": "success", "members": [user]})
        assert actor == "alice"
        assert request.url.path == "/api/v1/users/1"
        store.revoke_token(tokens["alice"])
        return httpx.Response(200, json={"result": "success", "user": user})

    app = create_http_app(
        Authenticator(
            store, AuthConfig("https://mcp.example/mcp", "https://zulip.example")
        ),
        Executor(
            channel_policy=ChannelPolicy(jd_enabled=True),
            transport=httpx.MockTransport(upstream),
        ),
        json_response=True,
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://mcp.example"
        ) as client,
    ):

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

        async def call(headers, tool, arguments):
            return await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                },
            )

        alice, bob = await initialize("alice"), await initialize("bob")
        for actor, headers in (("alice", alice), ("bob", bob)):
            response = await call(headers, "zulip_resolve_user", {"query": actor})
            result = response.json()["result"]["structuredContent"]
            assert result["data"]["matches"][0]["email"] == f"{actor}@example"
            assert result["data"]["total"] == 1
        response = await call(
            alice,
            "zulip_get_user",
            {"user_id": 1, "include_custom_profile_fields": True},
        )
        assert response.json()["result"]["isError"] is True
        assert len(calls) == 3  # Revocation prevents the second profile-fields request.
        assert (await call(alice, "zulip_get_own_user", {})).status_code == 401
        response = await call(bob, "zulip_resolve_user", {"query": "bob"})
        assert (
            response.json()["result"]["structuredContent"]["data"]["status"]
            == "resolved"
        )

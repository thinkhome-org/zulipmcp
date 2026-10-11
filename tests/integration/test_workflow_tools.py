import time

import httpx
import pytest
from cryptography.fernet import Fernet

from thinkhome_zulip_mcp.auth import AuthConfig, Authenticator
from thinkhome_zulip_mcp.executor import Executor, Principal
from thinkhome_zulip_mcp.server import create_http_app
from thinkhome_zulip_mcp.store import IdentityStore


@pytest.mark.asyncio
async def test_http_workflows_bind_caller_and_stop_batch_on_revocation(tmp_path):
    store = IdentityStore(tmp_path / "db", Fernet.generate_key())
    tokens = {}
    for name in ("alice", "bob"):
        store.add_identity(Principal(name, "https://zulip.example", name, "synthetic"))
        tokens[name] = store.issue_token(name, expires_at=int(time.time()) + 100)
    calls = []

    def upstream(request):
        calls.append(request)
        if request.method == "POST":
            assert (
                request.headers["authorization"]
                == httpx.BasicAuth("alice", "synthetic")._auth_header
            )
            store.revoke_token(tokens["alice"])
            return httpx.Response(200, json={"result": "success", "id": 5})
        assert (
            request.headers["authorization"]
            == httpx.BasicAuth("bob", "synthetic")._auth_header
        )
        return httpx.Response(
            200,
            json={
                "result": "success",
                "messages": [{"id": 8}],
                "found_oldest": True,
                "history_limited": True,
            },
        )

    app = create_http_app(
        Authenticator(
            store, AuthConfig("https://mcp.example/mcp", "https://zulip.example")
        ),
        Executor(transport=httpx.MockTransport(upstream)),
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

        alice, bob = await initialize("alice"), await initialize("bob")
        operation = {
            "operation_id": "send-message",
            "arguments": {
                "type": "stream",
                "to": 3,
                "topic": "test",
                "content": "synthetic",
            },
        }
        response = await client.post(
            "/mcp",
            headers=alice,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "zulip_batch",
                    "arguments": {"operations": [operation, operation]},
                },
            },
        )
        result = response.json()["result"]["structuredContent"]
        assert result["error"]["code"] == "authentication_required"
        assert result["data"]["completed"] == 1
        assert result["data"]["not_attempted"] == [1]
        assert len(calls) == 1
        response = await client.post(
            "/mcp",
            headers=bob,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "zulip_history",
                    "arguments": {"narrow": [], "cursor": 10},
                },
            },
        )
        result = response.json()["result"]["structuredContent"]
        assert result["error"] is None
        assert result["data"]["next_cursor"] == 8
        assert result["data"]["history_limited"] is True
        assert len(calls) == 2

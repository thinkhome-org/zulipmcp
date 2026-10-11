import time

import httpx
import pytest
from cryptography.fernet import Fernet

from thinkhome_zulip_mcp.auth import AuthConfig, Authenticator
from thinkhome_zulip_mcp.executor import Executor, Principal
from thinkhome_zulip_mcp.server import create_http_app
from thinkhome_zulip_mcp.store import IdentityStore


@pytest.mark.asyncio
async def test_authenticated_http_read_only_discovery_and_execution(tmp_path):
    store = IdentityStore(tmp_path / "db", Fernet.generate_key())
    store.add_identity(
        Principal("alice", "https://zulip.example", "alice", "synthetic")
    )
    token = store.issue_token("alice", expires_at=int(time.time()) + 100)
    calls = []

    def upstream(request):
        calls.append(request)
        assert request.method == "GET"
        assert (
            request.headers["authorization"]
            == httpx.BasicAuth("alice", "synthetic")._auth_header
        )
        return httpx.Response(200, json={"result": "success", "messages": []})

    app = create_http_app(
        Authenticator(
            store, AuthConfig("https://mcp.example/mcp", "https://zulip.example")
        ),
        Executor(read_only=True, transport=httpx.MockTransport(upstream)),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://mcp.example"
        ) as client,
    ):
        headers = {
            "authorization": f"Bearer {token}",
            "accept": "application/json, text/event-stream",
        }

        async def rpc(method, params):
            response = await client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
            assert response.status_code == 200
            return response

        response = await rpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        )
        headers["mcp-session-id"] = response.headers["mcp-session-id"]
        headers["mcp-protocol-version"] = response.json()["result"]["protocolVersion"]
        await client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            },
        )
        response = await rpc("tools/list", {})
        names = {tool["name"] for tool in response.json()["result"]["tools"]}
        assert "zulip_send_message" not in names
        assert "zulip_register_events" not in names
        for operation in ("send-message", "get-events"):
            response = await rpc(
                "tools/call",
                {
                    "name": "zulip_execute",
                    "arguments": {"operation_id": operation, "arguments": {}},
                },
            )
            result = response.json()["result"]
            assert result["isError"]
            assert result["structuredContent"]["error"]["code"] == "read_only_policy"
        assert calls == []
        response = await rpc(
            "tools/call",
            {
                "name": "zulip_execute",
                "arguments": {
                    "operation_id": "get-messages",
                    "arguments": {
                        "anchor": "newest",
                        "num_before": 10,
                        "num_after": 0,
                    },
                },
            },
        )
        assert response.json()["result"]["structuredContent"]["error"] is None
        assert len(calls) == 1

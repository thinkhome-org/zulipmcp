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
async def test_http_channel_policy_covers_generic_batch_and_caller_history(tmp_path):
    store = IdentityStore(tmp_path / "db", Fernet.generate_key())
    tokens = {}
    for name in ("alice", "bob"):
        store.add_identity(Principal(name, "https://zulip.example", name, "synthetic"))
        tokens[name] = store.issue_token(name, expires_at=int(time.time()) + 300)
    calls = []
    auth_headers = {
        name: httpx.BasicAuth(name, "synthetic")._auth_header for name in tokens
    }

    def upstream(request):
        assert request.method == "GET", "A blocked write escaped the HTTP policy"
        auth = request.headers["authorization"]
        assert auth in auth_headers.values()
        calls.append((auth, request.url.path))
        if request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "found_oldest": True,
                    "found_newest": True,
                    "messages": [
                        {
                            "id": 10,
                            "type": "stream",
                            "stream_id": 8,
                            "content": "blocked",
                        },
                        {
                            "id": 11,
                            "type": "stream",
                            "stream_id": 7,
                            "content": "allowed",
                        },
                    ],
                },
            )
        stream_id = int(request.url.path.rsplit("/", 1)[1])
        assert stream_id in {7, 8}
        return httpx.Response(
            200,
            json={
                "result": "success",
                "stream": {
                    "stream_id": stream_id,
                    "name": "30 Work" if stream_id == 7 else "general",
                    "invite_only": auth == auth_headers["bob"],
                },
            },
        )

    app = create_http_app(
        Authenticator(
            store, AuthConfig("https://mcp.example/mcp", "https://zulip.example")
        ),
        Executor(
            channel_policy=ChannelPolicy.from_environment(
                {"ZULIPCHAT_CHANNEL_FILTER_ENABLED": "true"}
            ),
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

        async def rpc(headers, method, params):
            response = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": method,
                    "params": params,
                },
            )
            assert response.status_code == 200
            return response

        sessions = {}
        for name, token in tokens.items():
            headers = {
                "authorization": f"Bearer {token}",
                "accept": "application/json, text/event-stream",
            }
            response = await rpc(
                headers,
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            )
            headers["mcp-session-id"] = response.headers["mcp-session-id"]
            headers["mcp-protocol-version"] = response.json()["result"][
                "protocolVersion"
            ]
            await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                },
            )
            sessions[name] = headers

        write = {
            "operation_id": "send-message",
            "arguments": {
                "type": "stream",
                "to": 8,
                "topic": "test",
                "content": "must not send",
            },
        }
        response = await rpc(
            sessions["alice"],
            "tools/call",
            {
                "name": "zulip_execute",
                "arguments": write,
            },
        )
        result = response.json()["result"]
        assert result["isError"]
        assert result["structuredContent"]["error"]["code"] == "channel_policy_denied"
        response = await rpc(
            sessions["alice"],
            "tools/call",
            {
                "name": "zulip_send_message",
                "arguments": {"stream": 8, "topic": "test", "content": "must not send"},
            },
        )
        result = response.json()["result"]
        assert result["isError"]
        assert result["structuredContent"]["error"]["code"] == "channel_policy_denied"
        before_events = len(calls)
        response = await rpc(
            sessions["alice"],
            "tools/call",
            {
                "name": "zulip_register_events",
                "arguments": {"arguments": {}},
            },
        )
        result = response.json()["result"]
        assert result["isError"]
        assert (
            result["structuredContent"]["error"]["code"]
            == "channel_policy_not_supported"
        )
        assert len(calls) == before_events
        before_batch = len(calls)
        read = {"operation_id": "get-stream-by-id", "arguments": {"stream_id": 7}}
        response = await rpc(
            sessions["alice"],
            "tools/call",
            {
                "name": "zulip_batch",
                "arguments": {"operations": [read, write, read]},
            },
        )
        result = response.json()["result"]
        assert result["isError"]
        batch = result["structuredContent"]["data"]
        assert batch["completed"] == 1
        assert batch["not_attempted"] == [2]
        assert batch["results"][1]["result"]["error"]["code"] == "channel_policy_denied"
        assert len(calls) - before_batch == 2

        for name, expected in (("alice", [11]), ("bob", [])):
            before_history = len(calls)
            response = await rpc(
                sessions[name],
                "tools/call",
                {
                    "name": "zulip_history",
                    "arguments": {"narrow": [], "limit": 2},
                },
            )
            result = response.json()["result"]["structuredContent"]
            assert result["error"] is None
            assert [m["id"] for m in result["data"]["messages"]] == expected
            assert result["data"]["next_cursor"] == 10
            assert result["data"]["exhausted"] is True
            assert all(auth == auth_headers[name] for auth, _ in calls[before_history:])
            assert len(calls) - before_history == 3

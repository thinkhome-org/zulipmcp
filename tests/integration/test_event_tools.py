from urllib.parse import parse_qs

import httpx
import pytest

from thinkhome_zulip_mcp.channel_policy import ChannelPolicy
from thinkhome_zulip_mcp.executor import Executor, Principal
from thinkhome_zulip_mcp.server import create_server


@pytest.mark.asyncio
async def test_event_tools_use_catalog_wire_encoding_and_current_identity():
    seen = []

    def upstream(request):
        seen.append(request)
        assert (
            request.headers["authorization"]
            == httpx.BasicAuth("alice", "synthetic")._auth_header
        )
        if request.method == "POST":
            assert request.url.path == "/api/v1/register"
            assert parse_qs(request.content.decode()) == {
                "event_types": ['["message"]'],
                "narrow": ['[["stream","engineering"],["topic","example"]]'],
            }
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "queue_id": "server-queue",
                    "last_event_id": -1,
                },
            )
        assert request.url.path == "/api/v1/events"
        if request.method == "GET":
            assert request.url.params["queue_id"] == "server-queue"
            assert request.url.params["last_event_id"] == "-1"
            assert request.url.params["dont_block"] == "true"
            return httpx.Response(
                200,
                json={"result": "success", "events": [{"id": 3, "type": "message"}]},
            )
        assert request.method == "DELETE"
        assert parse_qs(request.content.decode()) == {"queue_id": ["server-queue"]}
        return httpx.Response(200, json={"result": "success"})

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream)),
    )
    result = await server.call_tool(
        "zulip_register_events",
        {
            "arguments": {
                "event_types": ["message"],
                "narrow": [["stream", "engineering"], ["topic", "example"]],
            }
        },
    )
    assert not result.is_error
    handle = result.structured_content["data"]["handle"]
    result = await server.call_tool(
        "zulip_poll_events", {"handle": handle, "last_event_id": -1, "wait_seconds": 0}
    )
    assert not result.is_error
    assert result.structured_content["data"]["next_event_id"] == 3
    result = await server.call_tool("zulip_delete_events", {"handle": handle})
    assert not result.is_error
    assert len(seen) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("filtered", [False, True])
async def test_http_queue_handles_cannot_cross_users_and_revocation_applies(
    tmp_path, filtered
):
    import time

    from cryptography.fernet import Fernet

    from thinkhome_zulip_mcp.auth import AuthConfig, Authenticator
    from thinkhome_zulip_mcp.server import create_http_app
    from thinkhome_zulip_mcp.store import IdentityStore

    store = IdentityStore(tmp_path / "db", Fernet.generate_key())
    tokens = {}
    for name in ("alice", "bob"):
        store.add_identity(Principal(name, "https://zulip.example", name, "synthetic"))
        tokens[name] = store.issue_token(name, expires_at=int(time.time()) + 100)
    calls = []

    def upstream(request):
        calls.append(request)
        assert (
            request.headers["authorization"]
            == httpx.BasicAuth("alice", "synthetic")._auth_header
        )
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "queue_id": "private-queue",
                    "last_event_id": -1,
                },
            )
        if request.url.path.endswith("/streams/7"):
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "stream": {
                        "stream_id": 7,
                        "name": "30 Work",
                        "invite_only": False,
                    },
                },
            )
        if request.method == "GET":
            assert request.url.params["last_event_id"] == "-1"
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "events": [
                        {
                            "id": 1,
                            "type": "message",
                            "message": {
                                "id": 10,
                                "type": "stream",
                                "stream_id": 7,
                                "content": "allowed",
                            },
                        },
                        {
                            "id": 2,
                            "type": "message",
                            "message": {
                                "id": 11,
                                "type": "private",
                                "content": "hidden",
                            },
                        },
                    ],
                },
            )
        assert request.method == "DELETE"
        return httpx.Response(200, json={"result": "success"})

    app = create_http_app(
        Authenticator(
            store, AuthConfig("https://mcp.example/mcp", "https://zulip.example")
        ),
        Executor(
            channel_policy=ChannelPolicy(jd_enabled=filtered),
            transport=httpx.MockTransport(upstream),
        ),
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
        registered = await client.post(
            "/mcp",
            headers=alice,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "zulip_register_events",
                    "arguments": {"arguments": {"event_types": ["message"]}},
                },
            },
        )
        handle = registered.json()["result"]["structuredContent"]["data"]["handle"]
        poll = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "zulip_poll_events",
                "arguments": {"handle": handle, "last_event_id": -1, "wait_seconds": 0},
            },
        }
        denied = (await client.post("/mcp", headers=bob, json=poll)).json()["result"]
        assert denied["isError"]
        assert denied["structuredContent"]["error"]["code"] == "invalid_queue_handle"
        assert len(calls) == 1
        owned = (await client.post("/mcp", headers=alice, json=poll)).json()["result"]
        assert not owned["isError"]
        data = owned["structuredContent"]["data"]
        assert [event["id"] for event in data["events"]] == (
            [1] if filtered else [1, 2]
        )
        assert data["next_event_id"] == 2
        if filtered:
            assert "hidden" not in str(data)
        deletion = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "zulip_delete_events", "arguments": {"handle": handle}},
        }
        before_delete = len(calls)
        denied_delete = (await client.post("/mcp", headers=bob, json=deletion)).json()[
            "result"
        ]
        assert (
            denied_delete["structuredContent"]["error"]["code"]
            == "invalid_queue_handle"
        )
        assert len(calls) == before_delete
        deleted = (await client.post("/mcp", headers=alice, json=deletion)).json()[
            "result"
        ]
        assert deleted["structuredContent"]["data"]["deleted"] is True
        assert len(calls) == before_delete + 1
        # Keep a live queue when revoking, to retain shutdown-cleanup coverage.
        registered_again = await client.post(
            "/mcp",
            headers=alice,
            json={
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "zulip_register_events",
                    "arguments": {"arguments": {"event_types": ["message"]}},
                },
            },
        )
        poll["params"]["arguments"]["handle"] = registered_again.json()["result"][
            "structuredContent"
        ]["data"]["handle"]
        before_revocation = len(calls)
        store.revoke_token(tokens["alice"])
        assert (await client.post("/mcp", headers=alice, json=poll)).status_code == 401
        assert len(calls) == before_revocation
    assert calls[-1].method == "DELETE"

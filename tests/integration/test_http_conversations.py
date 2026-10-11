import asyncio
import time
from itertools import count

import httpx
import pytest
from cryptography.fernet import Fernet

from thinkhome_zulip_mcp.auth import AuthConfig, Authenticator
from thinkhome_zulip_mcp.executor import Executor, Principal
from thinkhome_zulip_mcp.server import create_http_app
from thinkhome_zulip_mcp.store import IdentityStore


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "revoke_phase",
    [
        "before_reply",
        "during_context",
        "during_listen",
        "disconnect_then_end",
        "during_catchup",
    ],
)
async def test_http_conversation_handles_are_owned_and_revoked_tokens_cannot_reply(
    tmp_path,
    revoke_phase,
    monkeypatch,
):
    monkeypatch.setenv("SESSION_STREAM", "must-not-be-used-by-http")
    monkeypatch.delenv("SESSION_TOPIC", raising=False)
    monkeypatch.setenv("WORKSPACE_PATH", str(tmp_path / "must-not-be-created"))
    store = IdentityStore(tmp_path / "db", Fernet.generate_key())
    tokens = {}
    for name in ("alice", "bob"):
        store.add_identity(Principal(name, "https://zulip.example", name, "synthetic"))
        tokens[name] = store.issue_token(name, expires_at=int(time.time()) + 300)
    calls = []
    poll_entered = asyncio.Event()
    revoke_catchup = False
    headers_by_name = {
        name: httpx.BasicAuth(name, "synthetic")._auth_header for name in tokens
    }

    async def upstream(request):
        caller = next(
            name
            for name, header in headers_by_name.items()
            if header == request.headers["authorization"]
        )
        calls.append((caller, request.method, request.url.path))
        data = {"result": "success"}
        if request.url.path.endswith("/users/me"):
            if revoke_phase == "during_context" and caller == "alice":
                store.revoke_token(tokens["alice"])
            data |= {"user_id": 1 if caller == "alice" else 2, "max_message_id": 10}
        elif request.url.path.endswith("/streams/7"):
            data |= {"stream": {"stream_id": 7, "name": "work", "invite_only": False}}
        elif request.url.path.endswith("/messages") and request.method == "GET":
            data |= {"messages": [], "found_oldest": True, "found_newest": True}
            if revoke_catchup and caller == "alice":
                store.revoke_token(tokens["alice"])
                data |= {
                    "messages": [
                        {
                            "id": 11,
                            "type": "stream",
                            "stream_id": 7,
                            "subject": "topic",
                            "sender_id": 2,
                            "content": "sensitive catchup history",
                        }
                    ],
                    "found_newest": False,
                }
        elif request.url.path.endswith("/messages") and request.method == "POST":
            data |= {"id": 20}
        elif request.url.path.endswith("/users/1/subscriptions/7"):
            data |= {"is_subscribed": True}
        elif request.url.path.endswith("/realm/emoji"):
            data |= {"emoji": {}}
        elif request.url.path.endswith("/register"):
            data |= {"queue_id": "alice-queue", "last_event_id": -1}
        elif request.url.path.endswith("/events"):
            if request.method == "GET":
                if revoke_phase == "disconnect_then_end":
                    poll_entered.set()
                    await asyncio.Event().wait()
                store.revoke_token(tokens["alice"])
                data |= {
                    "events": [
                        {
                            "id": 0,
                            "type": "message",
                            "message": {
                                "id": 11,
                                "type": "stream",
                                "stream_id": 7,
                                "subject": "topic",
                                "sender_id": 2,
                                "content": "must not be delivered after revocation",
                            },
                        }
                    ]
                }
        elif request.url.path.endswith("/messages/10/reactions"):
            pass
        else:
            assert request.url.path.endswith("/typing")
        return httpx.Response(200, json=data)

    app = create_http_app(
        Authenticator(
            store, AuthConfig("https://mcp.example/mcp", "https://zulip.example")
        ),
        Executor(transport=httpx.MockTransport(upstream)),
        json_response=True,
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://mcp.example"
        ) as client,
    ):
        request_ids = count(1)

        async def rpc(headers, method, params):
            return await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": next(request_ids),
                    "method": method,
                    "params": params,
                },
            )

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
            assert response.status_code == 200
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

        async def call(name, tool, arguments):
            return await rpc(
                sessions[name], "tools/call", {"name": tool, "arguments": arguments}
            )

        handles = {}
        for name in tokens:
            response = await call(
                name, "zulip_set_context", {"stream": 7, "topic": "topic"}
            )
            result = response.json()["result"]
            if revoke_phase == "during_context":
                assert (
                    result["structuredContent"]["error"]["code"]
                    == "authentication_required"
                )
                assert calls == [("alice", "GET", "/api/v1/users/me")]
                return
            assert not result.get("isError")
            handles[name] = result["structuredContent"]["data"]["handle"]
        assert handles["alice"] != handles["bob"]
        if revoke_phase == "during_catchup":
            revoke_catchup = True
            response = await call(
                "alice", "zulip_reply", {"handle": handles["alice"], "content": "no"}
            )
            result = response.json()["result"]["structuredContent"]
            assert result["error"]["code"] == "authentication_required"
            assert result["data"] is None
            assert "sensitive catchup history" not in response.text
            assert ("alice", "POST", "/api/v1/messages") not in calls
            return
        if revoke_phase == "disconnect_then_end":
            task = asyncio.create_task(
                call(
                    "alice",
                    "zulip_listen",
                    {
                        "handle": handles["alice"],
                        "timeout_hours": 0.001,
                    },
                )
            )
            try:
                await asyncio.wait_for(poll_entered.wait(), 3)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 3)
                response = await call(
                    "alice",
                    "zulip_end_session",
                    {
                        "handle": handles["alice"],
                        "message": "",
                    },
                )
                assert "result" in response.json(), response.text
                assert response.json()["result"]["structuredContent"]["data"]["ended"]
                assert ("alice", "DELETE", "/api/v1/events") in calls
                assert ("alice", "DELETE", "/api/v1/messages/10/reactions") in calls
            finally:
                if not task.done():
                    task.cancel()
            return
        if revoke_phase == "during_listen":
            response = await call(
                "alice",
                "zulip_listen",
                {
                    "handle": handles["alice"],
                    "timeout_hours": 0.001,
                },
            )
            result = response.json()["result"]["structuredContent"]
            assert result["error"]["code"] == "authentication_required"
            assert result["data"] is None
            assert "must not be delivered" not in response.text
            assert ("alice", "DELETE", "/api/v1/events") in calls
            assert ("alice", "DELETE", "/api/v1/messages/10/reactions") in calls
            return
        before = len(calls)
        for tool, arguments in [
            ("zulip_reply", {"content": "must not send"}),
            ("zulip_listen", {"timeout_hours": 0.001}),
            ("zulip_end_session", {"message": ""}),
        ]:
            response = await call(
                "bob", tool, {"handle": handles["alice"], **arguments}
            )
            assert (
                response.json()["result"]["structuredContent"]["error"]["code"]
                == "invalid_conversation_handle"
            )
        assert len(calls) == before
        response = await call(
            "bob", "zulip_reply", {"handle": handles["bob"], "content": "answer"}
        )
        assert not response.json()["result"].get("isError")
        assert all(caller == "bob" for caller, _, _ in calls[before:])
        store.revoke_token(tokens["alice"])
        before = len(calls)
        response = await call(
            "alice", "zulip_reply", {"handle": handles["alice"], "content": "revoked"}
        )
        assert response.status_code == 401
        assert len(calls) == before
        response = await call(
            "bob", "zulip_end_session", {"handle": handles["bob"], "message": ""}
        )
        assert response.json()["result"]["structuredContent"]["data"]["ended"]

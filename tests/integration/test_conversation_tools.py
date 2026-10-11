import json
from contextlib import nullcontext
from urllib.parse import parse_qs

import httpx
import pytest

from thinkhome_zulip_mcp.executor import Executor, Principal
from thinkhome_zulip_mcp.server import create_server


@pytest.mark.asyncio
async def test_conversation_tools_share_executor_and_complete_a_conversation():
    calls = []

    def message(mid):
        return {
            "id": mid,
            "type": "stream",
            "stream_id": 7,
            "subject": "topic",
            "sender_id": 2,
            "content": "hello",
            "reactions": [],
        }

    def upstream(request):
        calls.append(request)
        assert (
            request.headers["authorization"]
            == httpx.BasicAuth("alice", "synthetic")._auth_header
        )
        path = request.url.path
        data = {"result": "success"}
        if path == "/api/v1/users/me":
            data |= {"user_id": 1, "max_message_id": 10}
        elif path == "/api/v1/streams/7":
            data |= {"stream": {"stream_id": 7, "name": "work", "invite_only": False}}
        elif path == "/api/v1/messages" and request.method == "GET":
            data |= {
                "messages": [message(10)]
                if request.url.params["anchor"] == "newest"
                else [],
                "found_oldest": True,
                "found_newest": True,
            }
        elif path == "/api/v1/messages" and request.method == "POST":
            assert parse_qs(request.content.decode())["to"] == ["7"]
            data |= {"id": 20}
        elif path == "/api/v1/users/1/subscriptions/7":
            data |= {"is_subscribed": True}
        elif path == "/api/v1/realm/emoji":
            data |= {"emoji": {}}
        elif path == "/api/v1/register":
            body = parse_qs(request.content.decode())
            assert json.loads(body["narrow"][0]) == [
                ["channel", "work"],
                ["topic", "topic"],
            ]
            data |= {"queue_id": "queue", "last_event_id": -1}
        elif path == "/api/v1/events" and request.method == "DELETE":
            assert parse_qs(request.content.decode()) == {"queue_id": ["queue"]}
        elif path == "/api/v1/events":
            data |= {"events": [{"id": 0, "type": "message", "message": message(11)}]}
        elif path not in {
            "/api/v1/typing",
            "/api/v1/messages/10/reactions",
        }:
            pytest.fail(f"Unexpected endpoint {request.method} {path}")
        return httpx.Response(200, json=data)

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream)),
    )
    created = await server.call_tool(
        "zulip_set_context", {"stream": 7, "topic": "topic"}
    )
    assert not created.is_error
    handle = created.structured_content["data"]["handle"]
    listened = await server.call_tool(
        "zulip_listen", {"handle": handle, "timeout_hours": 0.001}
    )
    assert not listened.is_error
    assert listened.structured_content["data"]["messages"][0]["id"] == 11
    replied = await server.call_tool(
        "zulip_reply", {"handle": handle, "content": "answer"}
    )
    assert not replied.is_error
    assert replied.structured_content["data"]["sent_message_id"] == 20
    ended = await server.call_tool(
        "zulip_end_session", {"handle": handle, "message": ""}
    )
    assert not ended.is_error
    assert ended.structured_content["data"]["ended"]
    assert any(
        request.method == "DELETE" and request.url.path == "/api/v1/events"
        for request in calls
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("startup", [False, True])
async def test_stdio_environment_context_can_be_used_without_explicit_handle(
    monkeypatch,
    startup,
):
    monkeypatch.setenv("SESSION_STREAM", "work")
    monkeypatch.setenv("SESSION_TOPIC", "")
    calls = []

    def upstream(request):
        calls.append(request.url.path)
        data = {"result": "success"}
        if request.url.path.endswith("/users/me"):
            data |= {"user_id": 1, "max_message_id": 10}
        elif request.url.path.endswith("/get_stream_id"):
            data |= {"stream_id": 7}
        elif request.url.path.endswith("/streams/7"):
            data |= {"stream": {"stream_id": 7, "name": "work", "invite_only": False}}
        elif request.url.path.endswith("/messages"):
            if request.method == "POST":
                data |= {"id": 20}
            else:
                data |= {"messages": [], "found_oldest": True, "found_newest": True}
        else:
            assert request.url.path.endswith("/typing")
        return httpx.Response(200, json=data)

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream)),
    )
    async with server.settings.lifespan(server) if startup else nullcontext():
        assert calls.count("/api/v1/users/me") == int(startup)
        context = await server.call_tool("zulip_set_context", {})
        assert not context.is_error
        assert context.structured_content["data"]["context"]["topic"] == ""
        reply = await server.call_tool("zulip_reply", {"content": "hello"})
        assert not reply.is_error
        assert calls.count("/api/v1/users/me") == 1
        ended = await server.call_tool("zulip_end_session", {"message": ""})
        assert not ended.is_error


@pytest.mark.parametrize(
    "env",
    [
        {"SESSION_STREAM": "work"},
        {"SESSION_STREAM": "", "SESSION_TOPIC": "topic"},
        {
            "SESSION_STREAM": "work",
            "SESSION_TOPIC": "topic",
            "TRIGGER_MESSAGE_ID": "-1",
        },
    ],
)
def test_invalid_automatic_context_configuration_rejected(monkeypatch, env):
    for key in ("SESSION_STREAM", "SESSION_TOPIC", "TRIGGER_MESSAGE_ID"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        create_server(Principal("alice", "https://zulip.example", "alice", "synthetic"))


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["sending", "hook", "typing"])
async def test_end_during_accepted_send_reports_uncertain_write(phase):
    import asyncio

    accepted = asyncio.Event()
    sends = []

    async def upstream(request):
        path = request.url.path
        data = {"result": "success"}
        if path.endswith("/users/me"):
            data |= {"user_id": 1, "max_message_id": 10}
        elif path.endswith("/streams/7"):
            data |= {"stream": {"stream_id": 7, "name": "work", "invite_only": False}}
        elif path.endswith("/messages") and request.method == "GET":
            data |= {"messages": [], "found_oldest": True, "found_newest": True}
        elif path.endswith("/messages") and request.method == "POST":
            sends.append(request.content)
            if phase == "sending":
                accepted.set()
                await asyncio.Event().wait()
            data |= {"id": 20}
        else:
            assert path.endswith("/typing")
            if (
                phase == "typing"
                and sends
                and parse_qs(request.content.decode())["op"] == ["start"]
            ):
                accepted.set()
                await asyncio.Event().wait()
        return httpx.Response(200, json=data)

    async def on_reply(mid, content):
        if phase == "hook":
            accepted.set()
            await asyncio.Event().wait()

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream)),
        conversation_options={"on_reply": on_reply},
    )
    context = await server.call_tool(
        "zulip_set_context", {"stream": 7, "topic": "topic"}
    )
    handle = context.structured_content["data"]["handle"]
    reply = asyncio.create_task(
        server.call_tool("zulip_reply", {"handle": handle, "content": "hello"})
    )
    try:
        await asyncio.wait_for(accepted.wait(), 3)
        ended = await server.call_tool(
            "zulip_end_session", {"handle": handle, "message": ""}
        )
        result = await asyncio.wait_for(reply, 3)
        assert ended.structured_content["data"]["ended"]
        assert result.structured_content["error"]["code"] == "conversation_cancelled"
        assert result.structured_content["uncertain_write"] is (phase == "sending")
        if phase != "sending":
            assert result.structured_content["data"]["sent_message_id"] == 20
        assert len(sends) == 1
        assert not (await server.call_tool("zulip_capabilities", {})).is_error
    finally:
        if not reply.done():
            reply.cancel()
            await asyncio.gather(reply, return_exceptions=True)

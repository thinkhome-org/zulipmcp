from urllib.parse import parse_qs

import httpx
import pytest

from thinkhome_zulip_mcp.executor import Executor, Principal
from thinkhome_zulip_mcp.server import create_server


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool, arguments, expected",
    [
        (
            "zulip_edit_message",
            {"message_id": 5, "content": "**test**"},
            {"content": ["**test**"]},
        ),
        (
            "zulip_move_messages",
            {"message_id": 5, "topic": "destination"},
            {
                "topic": ["destination"],
                "propagate_mode": ["change_one"],
                "send_notification_to_old_thread": ["true"],
                "send_notification_to_new_thread": ["true"],
            },
        ),
        (
            "zulip_resolve_topic",
            {"message_id": 5, "topic": "✔ done"},
            {
                "topic": ["✔ done"],
                "propagate_mode": ["change_all"],
                "send_notification_to_old_thread": ["false"],
                "send_notification_to_new_thread": ["false"],
            },
        ),
    ],
)
async def test_message_tools_preserve_content_and_notification_semantics(
    tool, arguments, expected
):
    seen = []

    def upstream(request):
        seen.append(request)
        assert request.method == "PATCH"
        assert request.url.path == "/api/v1/messages/5"
        assert parse_qs(request.content.decode()) == expected
        assert (
            request.headers["authorization"]
            == httpx.BasicAuth("alice", "synthetic")._auth_header
        )
        return httpx.Response(200, json={"result": "success"})

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream)),
    )
    result = await server.call_tool(tool, arguments)
    assert not result.is_error
    assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup_status", [200, 403])
async def test_cross_channel_move_resolves_name_before_write(lookup_status):
    seen = []

    def upstream(request):
        seen.append(request)
        if request.method == "GET":
            assert request.url.path == "/api/v1/get_stream_id"
            assert request.url.params["stream"] == "engineering"
            return httpx.Response(
                lookup_status,
                json={
                    "result": "success" if lookup_status == 200 else "error",
                    "stream_id": 7,
                },
            )
        assert parse_qs(request.content.decode())["stream_id"] == ["7"]
        return httpx.Response(200, json={"result": "success"})

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream)),
    )
    result = await server.call_tool(
        "zulip_move_messages",
        {
            "message_id": 5,
            "topic": "new",
            "stream": "engineering",
            "propagate_mode": "change_later",
        },
    )
    assert result.is_error == (lookup_status == 403)
    assert len(seen) == (2 if lookup_status == 200 else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_id", [None, True, -1, "7"])
async def test_move_rejects_malformed_lookup_without_writing(stream_id):
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"result": "success", "stream_id": stream_id})

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream)),
    )
    result = await server.call_tool(
        "zulip_move_messages",
        {"message_id": 5, "topic": "new", "stream": "engineering"},
    )
    assert result.is_error
    assert result.structured_content["error"]["code"] == "invalid_channel_response"
    assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool, arguments, method, path, encoded",
    [
        (
            "zulip_send_message",
            {"stream": "engineering", "topic": "test", "content": "hello"},
            "POST",
            "/api/v1/messages",
            {
                "type": ["stream"],
                "to": ['"engineering"'],
                "topic": ["test"],
                "content": ["hello"],
            },
        ),
        (
            "zulip_send_direct_message",
            {"recipients": [7, 8], "content": "hello"},
            "POST",
            "/api/v1/messages",
            {"type": ["direct"], "to": ["[7,8]"], "content": ["hello"]},
        ),
        (
            "zulip_get_message",
            {"message_id": 5},
            "GET",
            "/api/v1/messages/5",
            {"apply_markdown": ["false"], "allow_empty_topic_name": ["true"]},
        ),
    ],
)
async def test_basic_message_tools_use_caller_and_wire_contract(
    tool, arguments, method, path, encoded
):
    def upstream(request):
        assert request.method == method
        assert request.url.path == path
        assert (
            parse_qs(
                request.url.query.decode()
                if method == "GET"
                else request.content.decode()
            )
            == encoded
        )
        assert (
            request.headers["authorization"]
            == httpx.BasicAuth("alice", "synthetic")._auth_header
        )
        return httpx.Response(200, json={"result": "success", "id": 5})

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream)),
    )
    result = await server.call_tool(tool, arguments)
    assert not result.is_error
    assert result.structured_content["data"]["id"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool, method",
    [("zulip_add_reaction", "POST"), ("zulip_remove_reaction", "DELETE")],
)
async def test_reaction_tools_encode_custom_emoji(tool, method):
    def upstream(request):
        assert request.method == method
        assert request.url.path == "/api/v1/messages/5/reactions"
        assert parse_qs(request.content.decode()) == {
            "emoji_name": ["custom"],
            "emoji_code": ["123"],
            "reaction_type": ["realm_emoji"],
        }
        return httpx.Response(200, json={"result": "success"})

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream)),
    )
    result = await server.call_tool(
        tool,
        {
            "message_id": 5,
            "emoji_name": "custom",
            "emoji_code": "123",
            "reaction_type": "realm_emoji",
        },
    )
    assert not result.is_error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "autofix, expected",
    [
        ("1", "**[title](https://example.com)**"),
        ("0", "[**title**](https://example.com)"),
    ],
)
async def test_outgoing_message_normalization_can_be_disabled(
    monkeypatch, autofix, expected
):
    monkeypatch.setenv("ZULIPMCP_MARKDOWN_AUTOFIX", autofix)

    def upstream(request):
        assert parse_qs(request.content.decode())["content"] == [expected]
        return httpx.Response(200, json={"result": "success"})

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream)),
    )
    result = await server.call_tool(
        "zulip_edit_message",
        {"message_id": 5, "content": "[**title**](https://example.com)"},
    )
    assert not result.is_error


@pytest.mark.asyncio
async def test_configured_message_limit_blocks_before_network(monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    monkeypatch.setenv("ZULIP_MAX_MESSAGE_LENGTH", "3")
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"result": "success"})

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream)),
    )
    with pytest.raises(ToolError, match="limit"):
        await server.call_tool(
            "zulip_edit_message", {"message_id": 5, "content": "long"}
        )
    assert seen == []
    caps = await server.call_tool("zulip_capabilities", {})
    assert caps.structured_content["message_max_length"] == 3


@pytest.mark.parametrize("value", ["0", "-1", "invalid"])
def test_invalid_message_limit_rejects_startup(monkeypatch, value):
    monkeypatch.setenv("ZULIP_MAX_MESSAGE_LENGTH", value)
    with pytest.raises(ValueError):
        create_server(Principal("alice", "https://zulip.example", "alice", "synthetic"))

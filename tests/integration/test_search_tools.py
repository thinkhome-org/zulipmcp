import json

import httpx
import pytest

from thinkhome_zulip_mcp.executor import Executor, Principal
from thinkhome_zulip_mcp.server import create_server


@pytest.mark.asyncio
@pytest.mark.parametrize("sort", ["newest", "relevance"])
async def test_search_tool_uses_real_date_anchor_and_raw_markdown(sort):
    calls = []

    def upstream(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.path == "/api/v1/messages"
        assert (
            request.headers["authorization"]
            == httpx.BasicAuth("alice", "synthetic")._auth_header
        )
        args = request.url.params
        assert args["anchor"] == "date"
        assert args["anchor_date"] == "2026-10-11T10:00:01+00:00"
        assert args["apply_markdown"] == "false"
        assert json.loads(args["narrow"]) == [
            {"operator": "channel", "operand": 7},
            {"operator": "topic", "operand": ""},
            {"operator": "search", "operand": "hello"},
        ]
        return httpx.Response(
            200, json={"result": "success", "messages": [], "found_oldest": True}
        )

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream), read_only=True),
    )
    result = await server.call_tool(
        "zulip_search_messages",
        {
            "channel": 7,
            "topic": "",
            "query": "hello",
            "include_summary": True,
            "sort": sort,
            "before_time": "2026-10-11T12:00:00+02:00",
        },
    )
    assert not result.is_error
    assert result.structured_content["data"]["exhausted"] is True
    assert result.structured_content["data"]["summary"]["scope"] == "returned_sample"
    assert result.structured_content["data"]["summary"]["total_messages"] == 0
    assert len(calls) == 1
    assert result.structured_content["data"]["sort"] == sort


@pytest.mark.asyncio
async def test_message_context_tool_returns_authorized_link_and_source_record():
    def upstream(request):
        assert request.url.path == "/api/v1/messages/10"
        assert request.url.params["apply_markdown"] == "false"
        assert request.url.params["allow_empty_topic_name"] == "true"
        return httpx.Response(
            200,
            json={
                "result": "success",
                "message": {
                    "id": 10,
                    "type": "stream",
                    "stream_id": 7,
                    "subject": "",
                    "display_recipient": "work",
                    "content": "hello",
                    "sender_id": 2,
                },
            },
        )

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream), read_only=True),
    )
    result = await server.call_tool("zulip_message_context", {"message_id": 10})
    assert not result.is_error
    data = result.structured_content["data"]
    assert data["context"]["topic"] == ""
    assert data["url"] == "https://zulip.example/#narrow/channel/7/topic//near/10"
    assert json.loads(data["verified_message"])["message"]["id"] == 10


@pytest.mark.asyncio
async def test_context_tool_fetches_bounded_neighbors():
    calls = []
    target = {
        "id": 10,
        "type": "stream",
        "stream_id": 7,
        "subject": "",
        "display_recipient": "work",
        "sender_id": 2,
        "content": "hello",
    }

    def upstream(request):
        calls.append(request)
        if request.url.path.endswith("/messages/10"):
            return httpx.Response(200, json={"result": "success", "message": target})
        args = request.url.params
        assert args["anchor"] == "10"
        assert args["include_anchor"] == "false"
        assert args["allow_empty_topic_name"] == "true"
        assert args["apply_markdown"] == "false"
        assert json.loads(args["narrow"]) == [
            {"operator": "channel", "operand": 7},
            {"operator": "topic", "operand": ""},
        ]
        older = args["num_before"] == "1"
        return httpx.Response(
            200,
            json={
                "result": "success",
                "messages": [{**target, "id": 9 if older else 11}],
                "found_oldest": True,
                "found_newest": True,
            },
        )

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream)),
    )
    result = await server.call_tool(
        "zulip_message_context", {"message_id": 10, "before": 1, "after": 1}
    )
    assert not result.is_error
    assert [m["id"] for m in result.structured_content["data"]["messages"]] == [
        9,
        10,
        11,
    ]
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_construct_narrow_tool_is_local_and_preserves_negation():
    def upstream(request):
        pytest.fail("Pure narrow construction must not make network requests")

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream), read_only=True),
    )
    result = await server.call_tool(
        "zulip_construct_narrow", {"topic": "", "is_unread": False}
    )
    assert not result.is_error
    assert result.structured_content["result"] == [
        {"operator": "topic", "operand": ""},
        {"operator": "is", "operand": "unread", "negated": True},
    ]


@pytest.mark.asyncio
async def test_check_matches_tool_uses_deduplicated_ids():
    def upstream(request):
        assert request.url.path == "/api/v1/messages/matches_narrow"
        assert json.loads(request.url.params["msg_ids"]) == [10, 11]
        return httpx.Response(
            200,
            json={
                "result": "success",
                "messages": {
                    "10": {"match_content": "hello", "match_subject": "topic"},
                },
            },
        )

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream), read_only=True),
    )
    result = await server.call_tool(
        "zulip_check_matches", {"message_ids": [10, 11, 10], "narrow": []}
    )
    assert not result.is_error
    assert result.structured_content["data"]["matching_message_ids"] == [10]
    assert result.structured_content["data"]["non_matching_count"] == 1


@pytest.mark.asyncio
async def test_topic_search_tool_continues_and_preserves_empty_topic():
    def upstream(request):
        assert request.url.path == "/api/v1/users/me/7/topics"
        assert request.url.params["allow_empty_topic_name"] == "true"
        return httpx.Response(
            200,
            json={
                "result": "success",
                "topics": [{"name": "Zebra", "max_id": 12}, {"name": "", "max_id": 10}],
            },
        )

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream), read_only=True),
    )
    first = await server.call_tool(
        "zulip_search_topics",
        {
            "query": "",
            "stream_ids": [7],
            "limit": 1,
        },
    )
    assert not first.is_error
    data = first.structured_content["data"]
    assert data["topics"] == [{"stream_id": 7, "name": "", "max_id": 10}]
    second = await server.call_tool(
        "zulip_search_topics",
        {
            "query": "",
            "stream_ids": [7],
            "limit": 1,
            "cursor": data["next_cursor"],
        },
    )
    assert not second.is_error
    assert second.structured_content["data"]["topics"][0]["name"] == "Zebra"
    assert second.structured_content["data"]["exhausted"] is True


@pytest.mark.asyncio
async def test_advanced_search_tool_returns_directory_and_section_error():
    def upstream(request):
        if request.url.path == "/api/v1/users":
            return httpx.Response(200, json={"result": "success", "members": []})
        assert request.url.path == "/api/v1/streams"
        return httpx.Response(403, json={"result": "error", "msg": "Denied"})

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream), read_only=True),
    )
    result = await server.call_tool(
        "zulip_advanced_search",
        {
            "query": "hello",
            "sections": ["users", "streams"],
        },
    )
    assert not result.is_error
    data = result.structured_content["data"]
    assert data["partial"] is True
    assert data["sections"]["users"]["data"]["matches"] == []
    assert data["sections"]["streams"]["error"]


@pytest.mark.asyncio
async def test_multi_history_tool_uses_public_narrow_and_fixed_time():
    def upstream(request):
        assert request.url.path == "/api/v1/messages"
        args = request.url.params
        assert json.loads(args["narrow"]) == [
            {"operator": "channels", "operand": "public"}
        ]
        assert args["anchor"] == "date"
        assert args["anchor_date"] == "2026-10-11T12:00:01+00:00"
        return httpx.Response(
            200,
            json={
                "result": "success",
                "messages": [],
                "found_oldest": True,
                "found_newest": True,
                "found_anchor": False,
                "history_limited": False,
            },
        )

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream), read_only=True),
    )
    result = await server.call_tool(
        "zulip_multi_channel_history",
        {
            "as_of": "2026-10-11T12:00:00Z",
            "include_formatted": True,
            "participant_id": 10,
            "timezone": "Europe/Prague",
        },
    )
    assert not result.is_error
    assert result.structured_content["data"]["exhausted"] is True
    assert result.structured_content["data"]["context_messages"] == []
    assert isinstance(result.structured_content["data"]["formatted"], str)


@pytest.mark.asyncio
async def test_multi_history_wire_keeps_all_channels_and_resumes_each():
    from datetime import datetime

    stamp = int(datetime.fromisoformat("2026-10-11T11:00:00+00:00").timestamp())
    seen = []

    def upstream(request):
        args = request.url.params
        channel = json.loads(args["narrow"])[0]["operand"]
        seen.append((channel, args["anchor"]))
        if args["anchor"] == "date":
            ids = [2, 4] if channel == 7 else [3, 5]
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "messages": [{"id": mid, "timestamp": stamp} for mid in ids],
                    "found_oldest": False,
                },
            )
        assert args["anchor"] == ("2" if channel == 7 else "3")
        return httpx.Response(
            200, json={"result": "success", "messages": [], "found_oldest": True}
        )

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream), read_only=True),
    )
    arguments = {
        "channels": [7, 8],
        "as_of": "2026-10-11T12:00:00Z",
        "per_channel_limit": 2,
    }
    first = await server.call_tool("zulip_multi_channel_history", arguments)
    assert not first.is_error
    data = first.structured_content["data"]
    assert [row["id"] for row in data["messages"]] == [2, 3, 4, 5]
    second = await server.call_tool(
        "zulip_multi_channel_history",
        {
            **arguments,
            "as_of": data["as_of"],
            "cursors": data["cursors"],
        },
    )
    assert not second.is_error
    assert second.structured_content["data"]["exhausted"] is True
    assert seen == [(7, "date"), (8, "date"), (7, "2"), (8, "3")]


@pytest.mark.asyncio
async def test_search_formats_topic_history_and_preserves_raw_content():
    def upstream(request):
        assert json.loads(request.url.params["narrow"]) == [
            {"operator": "channel", "operand": 7},
            {"operator": "topic", "operand": ""},
        ]
        return httpx.Response(
            200,
            json={
                "result": "success",
                "found_oldest": True,
                "messages": [
                    {
                        "id": 3,
                        "sender_id": 2,
                        "sender_full_name": "Alice",
                        "type": "stream",
                        "stream_id": 7,
                        "display_recipient": "eng",
                        "subject": "",
                        "timestamp": 0,
                        "content": "<hello>&",
                    }
                ],
            },
        )

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream), read_only=True),
    )
    result = await server.call_tool(
        "zulip_search_messages",
        {
            "channel": 7,
            "topic": "",
            "include_formatted": True,
            "timezone": "Europe/Prague",
        },
    )
    assert not result.is_error
    data = result.structured_content["data"]
    assert data["messages"][0]["content"] == "<hello>&"
    assert "&lt;hello&gt;&amp;" in data["formatted"]
    assert "CET" in data["formatted"]

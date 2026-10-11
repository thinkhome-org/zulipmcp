from unittest.mock import AsyncMock

import pytest

from thinkhome_zulip_mcp.channel_policy import ChannelPolicy
from thinkhome_zulip_mcp.executor import OperationResult, Principal

CALLER = Principal("alice", "https://zulip.example", "alice", "synthetic")


@pytest.mark.asyncio
async def test_batch_reports_partial_success():
    from thinkhome_zulip_mcp.workflows import execute_batch

    executor = AsyncMock()
    executor.channel_policy = ChannelPolicy()
    executor.execute.side_effect = [
        OperationResult("first", data={"id": 1}),
        OperationResult("second", error={"code": "timeout"}, uncertain_write=True),
    ]
    operations = [{"operation_id": "send-message", "arguments": {}}] * 3
    result = await execute_batch(executor, lambda: CALLER, operations)
    assert result.error == {"code": "batch_stopped"}
    assert result.uncertain_write is True
    assert result.data["completed"] == 1
    assert result.data["not_attempted"] == [2]
    assert [r["result"]["request_id"] for r in result.data["results"]] == [
        "first",
        "second",
    ]
    assert executor.execute.await_count == 2
    assert all(call.args[0] == CALLER for call in executor.execute.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operations",
    [
        [],
        [{}] * 51,
        [{"operation_id": "x", "arguments": {}, "principal": "admin"}],
        [{"operation_id": "x", "arguments": []}],
    ],
)
async def test_invalid_batch_never_starts(operations):
    from thinkhome_zulip_mcp.workflows import execute_batch

    executor = AsyncMock()
    executor.channel_policy = ChannelPolicy()
    result = await execute_batch(executor, lambda: CALLER, operations)
    assert result.error["code"] == "invalid_batch"
    executor.execute.assert_not_called()


@pytest.mark.asyncio
async def test_batch_rechecks_identity_before_each_operation():
    from thinkhome_zulip_mcp.store import AuthenticationError
    from thinkhome_zulip_mcp.workflows import execute_batch

    count = 0

    def principal():
        nonlocal count
        count += 1
        if count == 2:
            raise AuthenticationError("Revoked")
        return CALLER

    executor = AsyncMock()
    executor.channel_policy = ChannelPolicy()
    executor.execute.return_value = OperationResult("first", data={})
    result = await execute_batch(
        executor, principal, [{"operation_id": "x", "arguments": {}}] * 3
    )
    assert result.data["completed"] == 1
    assert result.data["not_attempted"] == [1, 2]
    assert result.error["code"] == "authentication_required"
    executor.execute.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "direction, pages",
    [
        ("older", [[9, 14], [2, 5], []]),
        ("newer", [[2, 5], [9, 14], []]),
    ],
)
async def test_history_cursor_does_not_skip_messages(direction, pages):
    from thinkhome_zulip_mcp.workflows import history_page

    executor = AsyncMock()
    executor.channel_policy = ChannelPolicy()
    executor.execute.side_effect = [
        OperationResult(
            str(i),
            data={
                "messages": [{"id": mid} for mid in ids],
                "found_oldest": i == 2,
                "found_newest": i == 2,
                "history_limited": False,
            },
        )
        for i, ids in enumerate(pages)
    ]
    cursor = None
    seen = []
    for _ids in pages:
        result = await history_page(
            executor, CALLER, [], cursor=cursor, direction=direction, limit=2
        )
        assert result.error is None
        args = executor.execute.await_args.args[2]
        assert args["include_anchor"] is False
        assert args["anchor"] == (
            str(cursor)
            if cursor is not None
            else "newest"
            if direction == "older"
            else "oldest"
        )
        seen.extend(m["id"] for m in result.data["messages"])
        cursor = result.data["next_cursor"]
    assert sorted(seen) == [2, 5, 9, 14]
    assert result.data["exhausted"] is True
    assert executor.execute.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"messages": [], "found_oldest": False},
        {"messages": [{"id": 9}, {"id": 8}], "found_oldest": False},
        {"messages": [{"id": 10}], "found_oldest": False},
    ],
)
async def test_history_rejects_invalid_or_nonprogressing_response(payload):
    from thinkhome_zulip_mcp.workflows import history_page

    executor = AsyncMock()
    executor.channel_policy = ChannelPolicy()
    executor.execute.return_value = OperationResult("upstream", data=payload)
    result = await history_page(executor, CALLER, [], cursor=10)
    assert result.error["code"] == "invalid_history_response"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 201},
        {"limit": True},
        {"cursor": 0},
        {"cursor": True},
        {"direction": "sideways"},
    ],
)
async def test_history_invalid_arguments_never_reach_upstream(kwargs):
    from thinkhome_zulip_mcp.workflows import history_page

    executor = AsyncMock()
    executor.channel_policy = ChannelPolicy()
    result = await history_page(executor, CALLER, [], **kwargs)
    assert result.error["code"] == "invalid_history_request"
    executor.execute.assert_not_called()


@pytest.mark.asyncio
async def test_history_wire_encoding_and_upstream_error():
    import json
    from urllib.parse import parse_qs

    import httpx

    from thinkhome_zulip_mcp.executor import Executor
    from thinkhome_zulip_mcp.workflows import history_page

    def respond(request):
        assert request.url.path == "/api/v1/messages"
        args = parse_qs(request.url.query.decode())
        assert args["anchor"] == ["42"]
        assert args["include_anchor"] == ["false"]
        assert args["num_before"] == ["0"]
        assert args["num_after"] == ["200"]
        assert json.loads(args["narrow"][0]) == [{"operator": "stream", "operand": 3}]
        return httpx.Response(403, json={"result": "error", "msg": "Forbidden"})

    executor = Executor(transport=httpx.MockTransport(respond))
    result = await history_page(
        executor,
        CALLER,
        [{"operator": "stream", "operand": 3}],
        cursor=42,
        direction="newer",
        limit=200,
    )
    assert result.status_code == 403
    assert result.error["code"] == "upstream_error"

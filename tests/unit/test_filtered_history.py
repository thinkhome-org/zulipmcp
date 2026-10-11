import httpx
import pytest

from thinkhome_zulip_mcp.channel_policy import ChannelPolicy
from thinkhome_zulip_mcp.executor import Executor, Principal
from thinkhome_zulip_mcp.workflows import history_page

CALLER = Principal("alice", "https://zulip.example", "alice", "synthetic")
POLICY = ChannelPolicy.from_environment({"ZULIPCHAT_CHANNEL_FILTER_ENABLED": "true"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "direction,start,hidden,visible",
    [
        ("older", 20, [18, 19], 17),
        ("newer", 10, [11, 12], 13),
    ],
)
async def test_history_advances_past_fully_filtered_page(
    direction, start, hidden, visible
):
    def upstream(request):
        assert (
            request.headers["authorization"]
            == httpx.BasicAuth("alice", "synthetic")._auth_header
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
        initial = request.url.params["anchor"] == str(start)
        messages = (
            [{"id": i, "type": "private", "content": "secret"} for i in hidden]
            if initial
            else [
                {"id": visible, "type": "stream", "stream_id": 7, "content": "visible"}
            ]
        )
        return httpx.Response(
            200,
            json={
                "result": "success",
                "messages": messages,
                "found_oldest": not initial,
                "found_newest": not initial,
                "history_limited": True,
            },
        )

    executor = Executor(channel_policy=POLICY, transport=httpx.MockTransport(upstream))
    page = await history_page(
        executor, CALLER, [], cursor=start, direction=direction, limit=2
    )
    assert page.error is None
    assert page.data["messages"] == []
    assert page.data["exhausted"] is False
    assert page.data["next_cursor"] == (
        hidden[0] if direction == "older" else hidden[-1]
    )
    next_page = await history_page(
        executor,
        CALLER,
        [],
        cursor=page.data["next_cursor"],
        direction=direction,
        limit=2,
    )
    assert next_page.error is None
    assert [m["id"] for m in next_page.data["messages"]] == [visible]
    assert next_page.data["exhausted"] is True
    assert next_page.data["history_limited"] is True


@pytest.mark.asyncio
async def test_page_checks_each_stream_once_and_discards_untrusted_cursor_metadata():
    lookups = []

    def upstream(request):
        if request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "messages": [
                        {"id": i, "type": "stream", "stream_id": 7, "content": "hidden"}
                        for i in [1, 2]
                    ],
                    "found_oldest": True,
                    "found_newest": True,
                    "policy_page": {"first_id": 999},
                    "unexpected": "secret",
                },
            )
        lookups.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "result": "success",
                "stream": {
                    "stream_id": 7,
                    "name": "30 Secret",
                    "invite_only": True,
                },
            },
        )

    executor = Executor(channel_policy=POLICY, transport=httpx.MockTransport(upstream))
    result = await executor.execute(
        CALLER, "get-messages", {"anchor": "newest", "num_before": 2, "num_after": 0}
    )
    assert result.error is None
    assert result.data["messages"] == []
    assert result.data["policy_page"] == {"first_id": 1, "last_id": 2, "count": 2}
    assert "unexpected" not in result.data
    assert len(lookups) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("ids", [[True], [0], [2, 1], [1, 1], [None]])
async def test_malformed_message_ids_fail_without_cursor(ids):
    executor = Executor(
        channel_policy=POLICY,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "result": "success",
                    "messages": [{"id": mid, "type": "private"} for mid in ids],
                    "found_oldest": True,
                },
            )
        ),
    )
    result = await history_page(executor, CALLER, [])
    assert result.error["code"] == "invalid_upstream_response"
    assert result.data is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,metadata",
    [
        (503, {"result": "error", "msg": "unavailable"}),
        (403, {"result": "error", "msg": "denied"}),
        (
            200,
            {
                "result": "success",
                "stream": {"stream_id": 8, "name": "30 Work", "invite_only": False},
            },
        ),
        (
            200,
            {
                "result": "success",
                "stream": {"stream_id": 7, "name": "30 Work", "invite_only": "false"},
            },
        ),
    ],
)
async def test_metadata_failure_does_not_silently_skip_page(status, metadata):
    def upstream(request):
        if request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "messages": [
                        {
                            "id": 10,
                            "type": "stream",
                            "stream_id": 7,
                            "content": "secret",
                        }
                    ],
                    "found_oldest": True,
                },
            )
        return httpx.Response(status, json=metadata)

    executor = Executor(channel_policy=POLICY, transport=httpx.MockTransport(upstream))
    result = await history_page(executor, CALLER, [])
    assert result.error["code"] == "channel_policy_lookup_failed"
    assert result.data is None


@pytest.mark.asyncio
async def test_filter_metadata_is_not_reused_between_callers():
    seen = []

    def upstream(request):
        if request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "messages": [{"id": 10, "type": "stream", "stream_id": 7}],
                    "found_oldest": True,
                },
            )
        auth = request.headers["authorization"]
        seen.append(auth)
        return httpx.Response(
            200,
            json={
                "result": "success",
                "stream": {
                    "stream_id": 7,
                    "name": "30 Work",
                    "invite_only": auth
                    != httpx.BasicAuth("alice", "synthetic")._auth_header,
                },
            },
        )

    executor = Executor(channel_policy=POLICY, transport=httpx.MockTransport(upstream))
    alice = await history_page(executor, CALLER, [])
    bob = await history_page(
        executor, Principal("bob", "https://zulip.example", "bob", "synthetic"), []
    )
    assert len(alice.data["messages"]) == 1
    assert bob.data["messages"] == []
    assert len(set(seen)) == 2


@pytest.mark.asyncio
async def test_filter_deadline_discards_entire_page(monkeypatch):
    import asyncio

    original_timeout = asyncio.timeout

    def immediate_timeout(seconds):
        assert seconds == 30
        return original_timeout(0)

    async def upstream(request):
        if request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "messages": [{"id": 10, "type": "stream", "stream_id": 7}],
                    "found_oldest": True,
                },
            )
        await asyncio.sleep(0)
        pytest.fail("Deadline should cancel metadata request")

    monkeypatch.setattr(asyncio, "timeout", immediate_timeout)
    executor = Executor(channel_policy=POLICY, transport=httpx.MockTransport(upstream))
    result = await history_page(executor, CALLER, [])
    assert result.error["code"] == "channel_policy_lookup_timeout"
    assert result.data is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bounds",
    [
        None,
        {"count": True, "first_id": 1, "last_id": 1},
        {"count": -1, "first_id": 1, "last_id": 2},
        {"count": 201, "first_id": 1, "last_id": 201},
        {"count": 0, "first_id": 1, "last_id": 1},
        {"count": 1, "first_id": 1, "last_id": 2},
        {"count": 2, "first_id": 3, "last_id": 2},
        {"count": 2, "first_id": 10, "last_id": 11},
    ],
)
async def test_history_rejects_invalid_or_nonadvancing_policy_bounds(bounds):
    from unittest.mock import AsyncMock

    from thinkhome_zulip_mcp.executor import OperationResult

    executor = AsyncMock()
    executor.channel_policy = POLICY
    executor.execute.return_value = OperationResult(
        "test",
        data={
            "messages": [],
            "found_oldest": False,
            "policy_page": bounds,
        },
    )
    result = await history_page(executor, CALLER, [], cursor=10)
    assert result.error["code"] == "invalid_history_response"
    assert result.data is None


@pytest.mark.asyncio
async def test_date_page_encodes_anchor_and_keeps_hidden_traversal_cursor():
    def upstream(request):
        assert request.method == "GET"
        assert request.url.path == "/api/v1/messages"
        assert request.url.params["anchor"] == "date"
        assert request.url.params["anchor_date"] == "2026-10-11T10:00:00+00:00"
        assert request.url.params["include_anchor"] == "true"
        assert request.url.params["apply_markdown"] == "false"
        assert request.url.params["allow_empty_topic_name"] == "true"
        assert request.url.params["num_before"] == "1"
        return httpx.Response(
            200,
            json={
                "result": "success",
                "messages": [
                    {"id": 9, "type": "private", "content": "hidden"},
                    {"id": 10, "type": "private", "content": "hidden"},
                ],
                "found_oldest": False,
                "found_newest": False,
            },
        )

    executor = Executor(channel_policy=POLICY, transport=httpx.MockTransport(upstream))
    page = await history_page(
        executor,
        CALLER,
        [],
        limit=2,
        anchor_date="2026-10-11T12:00:00+02:00",
        apply_markdown=False,
    )
    assert page.error is None
    assert page.data["messages"] == []
    assert page.data["next_cursor"] == 9
    assert page.data["exhausted"] is False

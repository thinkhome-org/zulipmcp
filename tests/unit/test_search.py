import pytest

from thinkhome_zulip_mcp.search import construct_narrow


def test_narrow_preserves_empty_topic_and_false_filters():
    assert construct_narrow(channel=7, topic="", has_link=False, is_unread=True) == [
        {"operator": "channel", "operand": 7},
        {"operator": "topic", "operand": ""},
        {"operator": "has", "operand": "link", "negated": True},
        {"operator": "is", "operand": "unread"},
    ]


def test_narrow_combines_terms_without_mutating_input():
    source = [{"operator": "dm", "operand": [1, 2], "negated": False}]
    result = construct_narrow(
        narrow=source, sender="žan@example.org", search_text="hello"
    )
    result[0]["operand"].append(3)
    assert source[0]["operand"] == [1, 2]
    assert result[1:] == [
        {"operator": "sender", "operand": "žan@example.org"},
        {"operator": "search", "operand": "hello"},
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        {"channel": True},
        {"channel": 0},
        {"channel": ""},
        {"sender": []},
        {"topic": 2},
        {"has_image": 1},
        {"message_id": -1},
        {"near_message_id": True},
        {"dm_with": []},
        {"dm_with": [True]},
        {"group_dm_with": [0]},
        {"narrow": [{"operator": "is", "operand": "unread", "negated": "yes"}]},
        {"narrow": [{"operator": "is", "operand": "unread", "extra": 1}]},
    ],
)
def test_narrow_rejects_invalid_values(arguments):
    with pytest.raises(ValueError):
        construct_narrow(**arguments)


def test_narrow_supports_ids_dm_and_all_state_filters():
    result = construct_narrow(
        message_id=12,
        near_message_id=11,
        dm_with=[1, 2],
        group_dm_with="person@example.org",
        is_private=False,
        is_starred=True,
        is_mentioned=False,
        is_muted=True,
        is_followed=False,
        has_attachment=True,
        has_image=False,
    )
    assert {"operator": "id", "operand": 12} in result
    assert {"operator": "near", "operand": 11} in result
    assert {"operator": "dm", "operand": [1, 2]} in result
    assert {"operator": "group-pm-with", "operand": "person@example.org"} in result
    assert {"operator": "is", "operand": "dm", "negated": True} in result
    assert len(result) == 11


def test_time_bounds_normalize_offsets_and_intersect_relative_window():
    from datetime import UTC, datetime

    from thinkhome_zulip_mcp.search import time_bounds

    lower, upper = time_bounds(
        after_time="2026-10-11T08:00:00+02:00",
        before_time="2026-10-11T12:00:00+02:00",
        last_hours=2,
        now=datetime(2026, 10, 11, 11, tzinfo=UTC),
    )
    assert lower == datetime(2026, 10, 11, 9, tzinfo=UTC)
    assert upper == datetime(2026, 10, 11, 10, tzinfo=UTC)


def test_time_bounds_as_of_and_hours_back_are_relative_to_snapshot():
    from datetime import UTC, datetime

    from thinkhome_zulip_mcp.search import time_bounds

    assert time_bounds(as_of="2020-01-02T00:00:00Z", hours_back=24) == (
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2020, 1, 2, tzinfo=UTC),
    )
    assert time_bounds() == (None, None)
    assert time_bounds(after_time="2020-01-01")[0] == datetime(2020, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "arguments",
    [
        {"last_hours": float("nan")},
        {"last_days": float("inf")},
        {"hours_back": -1},
        {"last_hours": True},
        {"last_days": 0},
        {"after_time": "bad"},
        {"before_time": 0},
        {"after_time": "2021-01-01", "before_time": "2020-01-01"},
        {"hours_back": 1e300},
        {"as_of": "2020-01-01", "after_time": "2021-01-01"},
    ],
)
def test_time_bounds_reject_invalid_or_empty_intervals(arguments):
    from thinkhome_zulip_mcp.search import time_bounds

    with pytest.raises(ValueError):
        time_bounds(**arguments)


@pytest.mark.asyncio
async def test_search_pages_through_time_filtered_rows_without_skipping():
    from unittest.mock import AsyncMock

    from thinkhome_zulip_mcp.channel_policy import ChannelPolicy
    from thinkhome_zulip_mcp.executor import OperationResult, Principal
    from thinkhome_zulip_mcp.search import search_messages

    caller = Principal("alice", "https://zulip.example", "alice", "synthetic")
    executor = AsyncMock()
    executor.channel_policy = ChannelPolicy()
    executor.execute.side_effect = [
        OperationResult(
            "1",
            data={
                "messages": [
                    {"id": 9, "timestamp": 900},
                    {"id": 10, "timestamp": 1000},
                ],
                "found_oldest": False,
            },
        ),
        OperationResult(
            "2",
            data={
                "messages": [{"id": 5, "timestamp": 500}, {"id": 8, "timestamp": 800}],
                "found_oldest": True,
            },
        ),
    ]
    result = await search_messages(
        executor, lambda: caller, [], limit=2, before_time="1970-01-01T00:13:20Z"
    )
    assert not result.error
    assert [m["id"] for m in result.data["messages"]] == [8, 5]
    assert result.data["exhausted"] is True
    calls = executor.execute.await_args_list
    assert calls[0].args[2]["anchor"] == "date"
    assert calls[1].args[2]["anchor"] == "9"
    assert all(call.args[2]["apply_markdown"] is False for call in calls)


@pytest.mark.asyncio
async def test_search_bounds_work_and_exposes_continuation():
    from unittest.mock import AsyncMock

    from thinkhome_zulip_mcp.channel_policy import ChannelPolicy
    from thinkhome_zulip_mcp.executor import OperationResult, Principal
    from thinkhome_zulip_mcp.search import search_messages

    caller = Principal("alice", "https://zulip.example", "alice", "synthetic")
    executor = AsyncMock()
    executor.channel_policy = ChannelPolicy()
    executor.execute.return_value = OperationResult(
        "1",
        data={
            "messages": [{"id": 9, "timestamp": 900}],
            "found_oldest": False,
            "history_limited": True,
        },
    )
    result = await search_messages(
        executor,
        lambda: caller,
        [],
        limit=1,
        max_pages=1,
        before_time="1970-01-01T00:01:00Z",
    )
    assert result.data["messages"] == []
    assert result.data["next_cursor"] == 9
    assert result.data["has_more"] is True
    assert result.data["history_limited"] is True
    executor.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_revoked_during_fetch_returns_no_data():
    from unittest.mock import AsyncMock

    from thinkhome_zulip_mcp.channel_policy import ChannelPolicy
    from thinkhome_zulip_mcp.executor import OperationResult, Principal
    from thinkhome_zulip_mcp.search import search_messages
    from thinkhome_zulip_mcp.store import AuthenticationError

    caller = Principal("alice", "https://zulip.example", "alice", "synthetic")
    executor = AsyncMock()
    executor.channel_policy = ChannelPolicy()
    revoked = False

    def current():
        if revoked:
            raise AuthenticationError("revoked")
        return caller

    async def execute(*args):
        nonlocal revoked
        revoked = True
        return OperationResult(
            "1", data={"messages": [{"id": 1, "timestamp": 10}], "found_oldest": True}
        )

    executor.execute.side_effect = execute
    result = await search_messages(executor, current, [])
    assert result.error["code"] == "authentication_required"
    assert result.data is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "timestamp,limited",
    [
        (True, False),
        ("100", False),
        (float("nan"), False),
        (10**400, False),
        (1, "false"),
        (1, None),
    ],
)
async def test_search_rejects_malformed_upstream_metadata(timestamp, limited):
    from unittest.mock import AsyncMock

    from thinkhome_zulip_mcp.channel_policy import ChannelPolicy
    from thinkhome_zulip_mcp.executor import OperationResult, Principal
    from thinkhome_zulip_mcp.search import search_messages

    caller = Principal("alice", "https://zulip.example", "alice", "synthetic")
    executor = AsyncMock()
    executor.channel_policy = ChannelPolicy()
    executor.execute.return_value = OperationResult(
        "1",
        data={
            "messages": [{"id": 1, "timestamp": timestamp}],
            "found_oldest": True,
            "history_limited": limited,
        },
    )
    result = await search_messages(executor, lambda: caller, [])
    assert result.error["code"] == "invalid_search_response"
    assert result.data is None


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous", [False, True])
async def test_search_resolves_sender_without_guessing_ambiguous_names(ambiguous):
    from unittest.mock import AsyncMock

    from thinkhome_zulip_mcp.channel_policy import ChannelPolicy
    from thinkhome_zulip_mcp.executor import OperationResult, Principal
    from thinkhome_zulip_mcp.search import search_messages

    caller = Principal("alice", "https://zulip.example", "alice", "synthetic")
    users = [{"user_id": 7, "full_name": "Sam Test", "email": "sam@example.org"}]
    if ambiguous:
        users.append(
            {"user_id": 8, "full_name": "Sam Other", "email": "other@example.org"}
        )
    executor = AsyncMock()
    executor.channel_policy = ChannelPolicy()
    executor.execute.side_effect = [
        OperationResult("users", data={"members": users}),
        OperationResult("messages", data={"messages": [], "found_oldest": True}),
    ]
    result = await search_messages(executor, lambda: caller, [], sender="Sam")
    if ambiguous:
        assert result.error["code"] == "ambiguous_sender"
        assert result.data["total"] == 2
        executor.execute.assert_awaited_once()
    else:
        assert not result.error
        assert {"operator": "sender", "operand": 7} in executor.execute.await_args.args[
            2
        ]["narrow"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "revoked", "unexpected"])
async def test_match_helper_validates_identity_and_returned_ids(outcome):
    from unittest.mock import AsyncMock

    from thinkhome_zulip_mcp.executor import OperationResult, Principal
    from thinkhome_zulip_mcp.search import check_matches
    from thinkhome_zulip_mcp.store import AuthenticationError

    caller = Principal("alice", "https://zulip.example", "alice", "synthetic")
    revoked = False
    executor = AsyncMock()

    def current():
        if revoked:
            raise AuthenticationError("revoked")
        return caller

    async def execute(*args):
        nonlocal revoked
        revoked = outcome == "revoked"
        return OperationResult(
            "1",
            data={
                "messages": {
                    "99" if outcome == "unexpected" else "10": {
                        "match_content": "hello",
                        "match_subject": "topic",
                    },
                }
            },
        )

    executor.execute.side_effect = execute
    result = await check_matches(executor, current, [10, 11, 10], [])
    if outcome == "success":
        assert result.data["matching_message_ids"] == [10]
        assert result.data["total_checked"] == 2
        assert result.data["non_matching_count"] == 1
    else:
        assert result.data is None
        assert result.error["code"] == (
            "authentication_required" if revoked else "invalid_match_response"
        )


@pytest.mark.asyncio
async def test_relevance_ranks_sample_and_keeps_traversal_cursor():
    from unittest.mock import AsyncMock

    from thinkhome_zulip_mcp.channel_policy import ChannelPolicy
    from thinkhome_zulip_mcp.executor import OperationResult, Principal
    from thinkhome_zulip_mcp.search import search_messages

    caller = Principal("alice", "https://zulip.example", "alice", "synthetic")
    executor = AsyncMock()
    executor.channel_policy = ChannelPolicy()
    executor.execute.return_value = OperationResult(
        "1",
        data={
            "messages": [
                {
                    "id": 8,
                    "timestamp": 80,
                    "content": "hello hello",
                    "subject": "hello",
                },
                {"id": 9, "timestamp": 90, "content": "hello", "subject": ""},
            ],
            "found_oldest": False,
        },
    )
    result = await search_messages(
        executor,
        lambda: caller,
        [{"operator": "search", "operand": "hello"}],
        sort="relevance",
        limit=2,
    )
    assert not result.error
    assert [m["id"] for m in result.data["messages"]] == [8, 9]
    assert result.data["next_cursor"] == 8
    assert result.data["ranking_scope"] == "retrieved_sample"
    assert result.data["relevance_scores"] == {"8": 4, "9": 1}
    assert executor.execute.await_args.args[2]["anchor"] == "newest"


@pytest.mark.asyncio
async def test_relevance_requires_positive_search_text():
    from unittest.mock import AsyncMock

    from thinkhome_zulip_mcp.search import search_messages

    executor = AsyncMock()
    result = await search_messages(executor, lambda: None, [], sort="relevance")
    assert result.error["code"] == "invalid_search_request"
    executor.execute.assert_not_called()


def test_search_summary_counts_sample_without_merging_direct_messages_into_channels():
    from thinkhome_zulip_mcp.search import summarize_search

    result = summarize_search(
        [
            {
                "id": 1,
                "sender_id": 7,
                "type": "stream",
                "stream_id": 3,
                "display_recipient": "work",
                "subject": "Topic",
            },
            {
                "id": 2,
                "sender_id": 7,
                "type": "stream",
                "stream_id": 3,
                "display_recipient": "work",
                "subject": "topic",
            },
            {"id": 3, "sender_id": 8, "type": "private"},
        ]
    )
    assert result["scope"] == "returned_sample"
    assert result["total_messages"] == 3
    assert result["count_by_user"] == {"7": 2, "8": 1}
    assert result["count_by_stream"] == {"3": 2}
    assert result["direct_messages"] == 1
    assert result["topics"] == [
        {"stream_id": 3, "topic": "Topic", "count": 2, "latest_message_id": 2}
    ]


@pytest.mark.parametrize(
    "messages",
    [
        [{"id": 1, "sender_id": True, "type": "private"}],
        [{"id": 1, "sender_id": 1, "type": "unknown"}],
        [{"id": 1, "sender_id": 1, "type": "stream", "stream_id": 3, "subject": None}],
        [{"id": 1, "sender_id": 1, "type": "private"}] * 2,
    ],
)
def test_search_summary_rejects_invalid_messages(messages):
    from thinkhome_zulip_mcp.search import summarize_search

    with pytest.raises(ValueError):
        summarize_search(messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["users", "streams"])
async def test_directory_search_pages_visible_matches(kind):
    from unittest.mock import AsyncMock

    from thinkhome_zulip_mcp.executor import OperationResult, Principal
    from thinkhome_zulip_mcp.search import search_directory

    caller = Principal("alice", "https://zulip.example", "alice", "synthetic")
    rows = (
        [
            {"user_id": n, "full_name": "Sam", "email": f"{n}@example.org"}
            for n in (2, 1)
        ]
        if kind == "users"
        else [{"stream_id": n, "name": "Sam", "description": "work"} for n in (2, 1)]
    )
    executor = AsyncMock()
    executor.execute.return_value = OperationResult(
        "1", data={"members" if kind == "users" else "streams": rows}
    )
    first = await search_directory(executor, lambda: caller, "sam", kind=kind, limit=1)
    assert first.data["total_matches"] == 2
    assert first.data["next_offset"] == 1
    assert first.data["matches"][0]["user_id" if kind == "users" else "stream_id"] == 1
    second = await search_directory(
        executor, lambda: caller, "sam", kind=kind, limit=1, offset=1
    )
    assert second.data["exhausted"] is True
    assert second.data["next_offset"] is None
    assert second.data["matches"][0]["user_id" if kind == "users" else "stream_id"] == 2


@pytest.mark.asyncio
async def test_directory_search_discards_response_after_identity_change():
    from unittest.mock import AsyncMock

    from thinkhome_zulip_mcp.executor import OperationResult, Principal
    from thinkhome_zulip_mcp.search import search_directory

    actor = Principal("alice", "https://zulip.example", "alice", "synthetic")
    executor = AsyncMock()

    async def execute(*args):
        nonlocal actor
        actor = Principal("bob", "https://zulip.example", "bob", "synthetic")
        return OperationResult(
            "1",
            data={
                "members": [
                    {"user_id": 7, "full_name": "Secret", "email": "secret@example.org"}
                ]
            },
        )

    executor.execute.side_effect = execute
    result = await search_directory(executor, lambda: actor, "", kind="users")
    assert result.error["code"] == "authentication_required"
    assert result.data is None


@pytest.mark.asyncio
async def test_topic_search_resumes_inside_channel_without_losing_matches():
    from unittest.mock import AsyncMock

    from thinkhome_zulip_mcp.executor import OperationResult, Principal
    from thinkhome_zulip_mcp.search import search_topics

    caller = Principal("alice", "https://zulip.example", "alice", "synthetic")
    executor = AsyncMock()
    executor.execute.return_value = OperationResult(
        "1",
        data={
            "topics": [
                {"name": "Beta", "max_id": 12},
                {"name": "Alpha", "max_id": 10},
            ]
        },
    )
    first = await search_topics(executor, lambda: caller, "", [7], limit=1)
    assert first.data["topics"] == [{"stream_id": 7, "name": "Alpha", "max_id": 10}]
    assert first.data["next_cursor"] == {"channel_index": 0, "topic_offset": 1}
    second = await search_topics(
        executor, lambda: caller, "", [7], limit=1, cursor=first.data["next_cursor"]
    )
    assert second.data["topics"] == [{"stream_id": 7, "name": "Beta", "max_id": 12}]
    assert second.data["next_cursor"] is None
    assert executor.execute.await_args.args[2]["allow_empty_topic_name"] is True


@pytest.mark.asyncio
async def test_topic_search_reports_per_channel_failure():
    from unittest.mock import AsyncMock

    from thinkhome_zulip_mcp.executor import OperationResult, Principal
    from thinkhome_zulip_mcp.search import search_topics

    caller = Principal("alice", "https://zulip.example", "alice", "synthetic")
    executor = AsyncMock()
    executor.execute.side_effect = [
        OperationResult("1", error={"code": "forbidden"}),
        OperationResult("2", data={"topics": [{"name": "", "max_id": 10}]}),
    ]
    result = await search_topics(executor, lambda: caller, "", [7, 8])
    assert result.data["partial"] is True
    assert result.data["errors"] == [{"stream_id": 7, "error": {"code": "forbidden"}}]
    assert result.data["topics"] == [{"stream_id": 8, "name": "", "max_id": 10}]


@pytest.mark.asyncio
async def test_topic_search_revocation_discards_prior_channels():
    from unittest.mock import AsyncMock

    from thinkhome_zulip_mcp.executor import OperationResult, Principal
    from thinkhome_zulip_mcp.search import search_topics
    from thinkhome_zulip_mcp.store import AuthenticationError

    caller = Principal("alice", "https://zulip.example", "alice", "synthetic")
    revoked = False
    executor = AsyncMock()

    def current():
        if revoked:
            raise AuthenticationError("revoked")
        return caller

    async def execute(principal, operation, arguments):
        nonlocal revoked
        revoked = arguments["stream_id"] == 8
        return OperationResult("1", data={"topics": [{"name": "secret", "max_id": 10}]})

    executor.execute.side_effect = execute
    result = await search_topics(executor, current, "", [7, 8])
    assert result.error["code"] == "authentication_required"
    assert result.data is None


@pytest.mark.asyncio
async def test_advanced_search_combines_sections_and_discovers_topics():
    from unittest.mock import AsyncMock

    from thinkhome_zulip_mcp.executor import OperationResult, Principal
    from thinkhome_zulip_mcp.search import advanced_search

    caller = Principal("alice", "https://zulip.example", "alice", "synthetic")
    executor = AsyncMock()

    async def execute(principal, operation, arguments):
        if operation == "get-users":
            return OperationResult(
                "u",
                data={
                    "members": [
                        {"user_id": 1, "full_name": "hello", "email": "a@example.org"}
                    ]
                },
            )
        if operation == "get-streams":
            return OperationResult(
                "s",
                data={
                    "streams": [{"stream_id": 7, "name": "general", "description": ""}]
                },
            )
        assert operation == "get-stream-topics"
        assert arguments["stream_id"] == 7
        return OperationResult("t", data={"topics": [{"name": "hello", "max_id": 9}]})

    executor.execute.side_effect = execute
    result = await advanced_search(
        executor, lambda: caller, "hello", sections=["users", "streams", "topics"]
    )
    assert result.error is None
    assert result.data["sections"]["users"]["data"]["total_matches"] == 1
    assert result.data["sections"]["streams"]["data"]["total_matches"] == 0
    assert result.data["sections"]["topics"]["data"]["topics"][0]["max_id"] == 9
    assert result.data["partial"] is False


@pytest.mark.asyncio
async def test_advanced_search_discards_earlier_sections_on_revocation():
    from unittest.mock import AsyncMock

    from thinkhome_zulip_mcp.executor import OperationResult, Principal
    from thinkhome_zulip_mcp.search import advanced_search

    caller = Principal("alice", "https://zulip.example", "alice", "synthetic")
    executor = AsyncMock()

    async def execute(principal, operation, arguments):
        nonlocal caller
        if operation == "get-users":
            return OperationResult("u", data={"members": []})
        caller = Principal("bob", "https://zulip.example", "bob", "synthetic")
        return OperationResult("s", data={"streams": []})

    executor.execute.side_effect = execute
    result = await advanced_search(
        executor, lambda: caller, "", sections=["users", "streams"]
    )
    assert result.error["code"] == "authentication_required"
    assert result.data is None


@pytest.mark.asyncio
async def test_advanced_topics_continue_across_directory_batches():
    from unittest.mock import AsyncMock

    from thinkhome_zulip_mcp.executor import OperationResult, Principal
    from thinkhome_zulip_mcp.search import advanced_search

    caller = Principal("alice", "https://zulip.example", "alice", "synthetic")
    executor = AsyncMock()

    async def execute(principal, operation, arguments):
        if operation == "get-streams":
            return OperationResult(
                "s",
                data={
                    "streams": [
                        {"stream_id": n, "name": str(n), "description": ""}
                        for n in range(1, 22)
                    ]
                },
            )
        return OperationResult(
            "t", data={"topics": [{"name": "hello", "max_id": arguments["stream_id"]}]}
        )

    executor.execute.side_effect = execute
    first = await advanced_search(
        executor, lambda: caller, "hello", sections=["topics"]
    )
    data = first.data["sections"]["topics"]["data"]
    assert len(data["topics"]) == 20
    assert data["next_cursor"] == {
        "directory_offset": 20,
        "channel_index": 0,
        "topic_offset": 0,
    }
    second = await advanced_search(
        executor,
        lambda: caller,
        "hello",
        sections=["topics"],
        cursors={"topics": data["next_cursor"]},
    )
    data = second.data["sections"]["topics"]["data"]
    assert [topic["stream_id"] for topic in data["topics"]] == [21]
    assert data["exhausted"] is True


@pytest.mark.asyncio
async def test_advanced_topic_discovery_auth_error_discards_sections():
    from unittest.mock import AsyncMock

    from thinkhome_zulip_mcp.executor import OperationResult, Principal
    from thinkhome_zulip_mcp.search import advanced_search

    caller = Principal("alice", "https://zulip.example", "alice", "synthetic")
    executor = AsyncMock()
    executor.execute.return_value = OperationResult(
        "s", error={"code": "authentication_required"}
    )
    result = await advanced_search(executor, lambda: caller, "", sections=["topics"])
    assert result.data is None
    assert result.error["code"] == "authentication_required"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options",
    [
        {"sections": []},
        {"sections": ["users", "users"]},
        {"sections": ["unknown"]},
        {"limit": True},
        {"sections": ["users"], "cursors": {"messages": 1}},
        {"sections": ["users"], "cursors": {"users": -1}},
        {"sections": ["topics"], "cursors": {"topics": {"directory_offset": 0}}},
        {"stream_ids": [True]},
        {"stream_ids": [1, 1]},
    ],
)
async def test_advanced_search_rejects_invalid_request_before_io(options):
    from unittest.mock import AsyncMock

    from thinkhome_zulip_mcp.search import advanced_search

    executor = AsyncMock()
    result = await advanced_search(
        executor, lambda: pytest.fail("No identity lookup"), "", **options
    )
    assert result.error
    executor.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("sort", ["newest", "relevance"])
@pytest.mark.parametrize("limit", [2, 100])
async def test_inclusive_upper_bound_keeps_all_messages_in_boundary_second(
    monkeypatch, sort, limit
):
    from datetime import datetime
    from unittest.mock import AsyncMock

    from thinkhome_zulip_mcp import search
    from thinkhome_zulip_mcp.executor import OperationResult, Principal

    rows = [
        {"id": mid, "timestamp": stamp, "content": "hello", "subject": ""}
        for mid, stamp in ((1, 99), (2, 100), (3, 100))
    ]

    async def page(
        executor, principal, narrow, *, anchor_date, cursor, limit, **kwargs
    ):
        # Zulip find_date_anchor picks the FIRST message >= date, then pages by ID.
        if anchor_date is not None:
            stamp = datetime.fromisoformat(anchor_date).timestamp()
            anchor = next(
                (row["id"] for row in rows if row["timestamp"] >= stamp), rows[-1]["id"]
            )
            available = [row for row in rows if row["id"] <= anchor]
        else:
            available = [row for row in rows if cursor is None or row["id"] < cursor]
        batch = available[-limit:]
        return OperationResult(
            "page",
            data={
                "messages": batch,
                "next_cursor": batch[0]["id"] if batch else cursor,
                "exhausted": len(batch) == len(available),
                "history_limited": False,
            },
        )

    monkeypatch.setattr(search, "history_page", page)
    caller = Principal("a", "https://zulip.example", "a", "synthetic")
    result = await search.search_messages(
        AsyncMock(),
        lambda: caller,
        [{"operator": "search", "operand": "hello"}],
        before_time="1970-01-01T00:01:40Z",
        sort=sort,
        limit=limit,
    )
    assert result.error is None
    ids = [row["id"] for row in result.data["messages"]]
    if not result.data["exhausted"]:
        following = await search.search_messages(
            AsyncMock(),
            lambda: caller,
            [{"operator": "search", "operand": "hello"}],
            before_time="1970-01-01T00:01:40Z",
            sort=sort,
            limit=limit,
            cursor=result.data["next_cursor"],
        )
        assert following.error is None
        ids.extend(row["id"] for row in following.data["messages"])
    assert ids == [3, 2, 1]

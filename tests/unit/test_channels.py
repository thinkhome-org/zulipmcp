from unittest.mock import AsyncMock

import pytest

from thinkhome_zulip_mcp.executor import OperationResult, Principal


@pytest.mark.asyncio
async def test_channel_info_resolves_name_and_exposes_optional_failure():
    from thinkhome_zulip_mcp.channels import channel_info

    caller = Principal("a", "https://zulip.example", "a", "synthetic")
    executor = AsyncMock()
    executor.execute.side_effect = [
        OperationResult("id", data={"stream_id": 7}),
        OperationResult("info", data={"stream": {"stream_id": 7, "name": "eng"}}),
        OperationResult("members", data={"subscribers": [2, 1]}),
        OperationResult("topics", error={"code": "upstream_error"}),
    ]
    result = await channel_info(
        executor,
        lambda: caller,
        name="eng",
        include_subscribers=True,
        include_topics=True,
    )
    assert result.error is None
    assert result.data["stream"]["stream_id"] == 7
    assert result.data["subscribers"] == [1, 2]
    assert result.data["partial"] is True
    assert result.data["errors"] == [
        {"section": "topics", "error": {"code": "upstream_error"}}
    ]


@pytest.mark.asyncio
async def test_channel_info_mismatch_stops_before_metadata_read():
    from thinkhome_zulip_mcp.channels import channel_info

    caller = Principal("a", "https://zulip.example", "a", "synthetic")
    executor = AsyncMock()
    executor.execute.return_value = OperationResult("id", data={"stream_id": 8})
    result = await channel_info(executor, lambda: caller, name="eng", stream_id=7)
    assert result.error["code"] == "channel_identity_mismatch"
    assert executor.execute.await_count == 1


@pytest.mark.asyncio
async def test_channel_info_revocation_discards_prior_metadata():
    from thinkhome_zulip_mcp.channels import channel_info

    caller = Principal("a", "https://zulip.example", "a", "synthetic")
    executor = AsyncMock()

    async def execute(principal, operation, arguments):
        nonlocal caller
        if operation == "get-stream-by-id":
            return OperationResult(
                "info", data={"stream": {"stream_id": 7, "name": "eng"}}
            )
        caller = Principal("b", "https://zulip.example", "b", "synthetic")
        return OperationResult("members", data={"subscribers": [1]})

    executor.execute.side_effect = execute
    result = await channel_info(
        executor, lambda: caller, stream_id=7, include_subscribers=True
    )
    assert result.data is None
    assert result.error["code"] == "authentication_required"


@pytest.mark.asyncio
async def test_channel_topics_page_by_recency_and_keep_empty_name():
    from thinkhome_zulip_mcp.channels import channel_info

    caller = Principal("a", "https://zulip.example", "a", "synthetic")
    executor = AsyncMock()
    executor.execute.side_effect = [
        OperationResult("info", data={"stream": {"stream_id": 7, "name": "eng"}}),
        OperationResult(
            "topics",
            data={
                "topics": [
                    {"name": "old", "max_id": 2},
                    {"name": "", "max_id": 9},
                ]
            },
        ),
    ]
    result = await channel_info(
        executor, lambda: caller, stream_id=7, include_topics=True, topic_limit=1
    )
    assert result.data["topics"] == [{"name": "", "max_id": 9}]
    assert result.data["topic_count"] == 2
    assert result.data["next_topic_offset"] == 1
    assert executor.execute.call_args.args[2]["allow_empty_topic_name"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [None, [True], [1, 1], [{"id": 1}]])
async def test_channel_malformed_subscribers_are_explicit_partial_errors(rows):
    from thinkhome_zulip_mcp.channels import channel_info

    caller = Principal("a", "https://zulip.example", "a", "synthetic")
    executor = AsyncMock()
    executor.execute.side_effect = [
        OperationResult("info", data={"stream": {"stream_id": 7, "name": "eng"}}),
        OperationResult("members", data={"subscribers": rows}),
    ]
    result = await channel_info(
        executor, lambda: caller, stream_id=7, include_subscribers=True
    )
    assert result.data["partial"] is True
    assert "subscribers" not in result.data
    assert result.data["errors"][0]["error"]["code"] == "invalid_channel_response"


@pytest.mark.asyncio
async def test_channel_list_forwards_filters_without_removing_public_subscriptions():
    from thinkhome_zulip_mcp.channels import list_channels

    caller = Principal("a", "https://zulip.example", "a", "synthetic")
    executor = AsyncMock()
    executor.execute.return_value = OperationResult(
        "list",
        data={
            "streams": [
                {"stream_id": 2, "name": "Public subscription", "invite_only": False},
                {"stream_id": 1, "name": "Private", "invite_only": True},
            ]
        },
    )
    first = await list_channels(executor, lambda: caller, include_public=False, limit=1)
    assert first.data["streams"][0]["stream_id"] == 1
    assert first.data["next_offset"] == 1
    second = await list_channels(
        executor, lambda: caller, include_public=False, offset=1, limit=1
    )
    assert second.data["streams"][0]["invite_only"] is False
    assert second.data["total_count"] == 2
    arguments = executor.execute.call_args.args[2]
    assert arguments["include_public"] is False
    assert arguments["include_subscribed"] is True


@pytest.mark.asyncio
async def test_channel_list_rechecks_reader_after_io():
    from thinkhome_zulip_mcp.channels import list_channels

    caller = Principal("a", "https://zulip.example", "a", "synthetic")
    executor = AsyncMock()

    async def execute(*args):
        nonlocal caller
        caller = Principal("b", "https://zulip.example", "b", "synthetic")
        return OperationResult("list", data={"streams": []})

    executor.execute.side_effect = execute
    result = await list_channels(executor, lambda: caller)
    assert result.data is None
    assert result.error["code"] == "authentication_required"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options",
    [
        {},
        {"name": " "},
        {"stream_id": True},
        {"stream_id": 0},
        {"stream_id": 7, "include_topics": "yes"},
        {"stream_id": 7, "topic_limit": 0},
        {"stream_id": 7, "topic_offset": -1},
    ],
)
async def test_channel_info_invalid_input_never_reads_identity(options):
    from thinkhome_zulip_mcp.channels import channel_info

    executor = AsyncMock()
    result = await channel_info(
        executor, lambda: pytest.fail("Invalid input reached identity"), **options
    )
    assert result.error["code"] == "invalid_channel_request"
    executor.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options",
    [
        {"include_all": 1},
        {"include_public": "false"},
        {"limit": True},
        {"limit": 1001},
        {"offset": -1},
    ],
)
async def test_channel_list_invalid_input_never_reads_identity(options):
    from thinkhome_zulip_mcp.channels import list_channels

    executor = AsyncMock()
    result = await list_channels(
        executor, lambda: pytest.fail("Invalid input reached identity"), **options
    )
    assert result.error["code"] == "invalid_channel_request"
    executor.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("directory_error", [False, True])
async def test_channel_member_profiles_preserve_unresolved_and_inactive(
    directory_error,
):
    from thinkhome_zulip_mcp.channels import channel_info

    caller = Principal("a", "https://zulip.example", "a", "synthetic")
    executor = AsyncMock()
    inactive = {"user_id": 2, "full_name": "Former member", "is_active": False}
    executor.execute.side_effect = [
        OperationResult("info", data={"stream": {"stream_id": 7, "name": "eng"}}),
        OperationResult("members", data={"subscribers": [2, 1]}),
        OperationResult("directory", error={"code": "upstream_error"})
        if directory_error
        else OperationResult("directory", data={"members": [inactive]}),
    ]
    result = await channel_info(
        executor, lambda: caller, stream_id=7, include_member_profiles=True
    )
    assert result.error is None
    assert result.data["subscribers"] == [1, 2]
    if directory_error:
        assert result.data["partial"]
        assert result.data["errors"] == [
            {"section": "member_profiles", "error": {"code": "upstream_error"}}
        ]
    else:
        assert result.data["member_profiles"] == [inactive]
        assert result.data["unresolved_subscriber_ids"] == [1]
    assert executor.execute.call_args.args[1:] == ("get-users", {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "members",
    [
        None,
        [{"user_id": True, "full_name": "x"}],
        [{"user_id": 1, "full_name": "x"}] * 2,
    ],
)
async def test_channel_member_profiles_reject_malformed_directory(members):
    from thinkhome_zulip_mcp.channels import channel_info

    caller = Principal("a", "https://zulip.example", "a", "synthetic")
    executor = AsyncMock()
    executor.execute.side_effect = [
        OperationResult("info", data={"stream": {"stream_id": 7, "name": "eng"}}),
        OperationResult("members", data={"subscribers": [1]}),
        OperationResult("directory", data={"members": members}),
    ]
    result = await channel_info(
        executor, lambda: caller, stream_id=7, include_member_profiles=True
    )
    assert result.data["partial"]
    assert "member_profiles" not in result.data
    assert result.data["subscribers"] == [1]


@pytest.mark.asyncio
async def test_channel_member_profiles_identity_change_discards_everything():
    from thinkhome_zulip_mcp.channels import channel_info

    caller = Principal("a", "https://zulip.example", "a", "synthetic")
    executor = AsyncMock()

    async def execute(principal, operation, arguments):
        nonlocal caller
        if operation == "get-stream-by-id":
            return OperationResult(
                "info", data={"stream": {"stream_id": 7, "name": "eng"}}
            )
        if operation == "get-subscribers":
            return OperationResult("members", data={"subscribers": [1]})
        caller = Principal("b", "https://zulip.example", "b", "synthetic")
        return OperationResult("directory", data={"members": []})

    executor.execute.side_effect = execute
    result = await channel_info(
        executor, lambda: caller, stream_id=7, include_member_profiles=True
    )
    assert result.data is None
    assert result.error["code"] == "authentication_required"


@pytest.mark.asyncio
async def test_channel_list_formats_selected_fields_without_losing_raw_data():
    from thinkhome_zulip_mcp.channels import list_channels

    caller = Principal("a", "https://zulip.example", "a", "synthetic")
    row = {
        "stream_id": 7,
        "name": "eng",
        "description": "Work",
        "invite_only": True,
        "date_created": 0,
        "settings": {"long": "x" * 100},
        "extra": "kept",
    }
    executor = AsyncMock()
    executor.execute.return_value = OperationResult("list", data={"streams": [row]})
    result = await list_channels(
        executor, lambda: caller, fields=["date_created", "settings", "missing"]
    )
    assert result.data["streams"] == [row]
    assert "[private]" in result.data["formatted"]
    assert "date_created=1970-01-01T00:00:00Z" in result.data["formatted"]
    assert "missing=None" in result.data["formatted"]
    assert "..." in result.data["formatted"]
    assert "x" * 100 not in result.data["formatted"]
    assert "extra=kept" not in result.data["formatted"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", ["name", [1], [""], ["name"] * 101])
async def test_channel_list_invalid_fields_fail_before_identity(fields):
    from thinkhome_zulip_mcp.channels import list_channels

    def current():
        pytest.fail("invalid fields reached identity")

    result = await list_channels(AsyncMock(), current, fields=fields)
    assert result.error["code"] == "invalid_channel_request"

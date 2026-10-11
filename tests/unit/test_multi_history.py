from unittest.mock import AsyncMock

import pytest

from thinkhome_zulip_mcp.executor import OperationResult, Principal


@pytest.mark.asyncio
async def test_multi_history_merges_all_rows_and_keeps_each_cursor(monkeypatch):
    from thinkhome_zulip_mcp import multi_history

    calls = []

    async def search(executor, current, narrow, **options):
        calls.append((narrow, options))
        current()
        channel = narrow[0]["operand"]
        ids = [4, 2] if channel == "eng" else [3, 2]
        return OperationResult(
            "s",
            data={
                "messages": [{"id": n} for n in ids],
                "next_cursor": min(ids),
                "exhausted": False,
                "history_limited": False,
            },
        )

    monkeypatch.setattr(multi_history, "search_messages", search)
    caller = Principal("a", "https://zulip.example", "a", "synthetic")
    result = await multi_history.multi_channel_history(
        AsyncMock(),
        lambda: caller,
        channels=["eng", "ops"],
        as_of="2026-10-11T12:00:00Z",
    )
    assert [row["id"] for row in result.data["messages"]] == [2, 3, 4]
    assert result.data["cursors"] == {"0": 2, "1": 2}
    assert calls[0][1]["before_time"] == calls[1][1]["before_time"]
    assert calls[0][1]["after_time"] == calls[1][1]["after_time"]


@pytest.mark.asyncio
async def test_multi_history_default_public_empty_and_completed(monkeypatch):
    from thinkhome_zulip_mcp import multi_history

    search = AsyncMock(
        return_value=OperationResult(
            "s",
            data={
                "messages": [],
                "next_cursor": None,
                "exhausted": True,
                "history_limited": False,
            },
        )
    )
    monkeypatch.setattr(multi_history, "search_messages", search)
    caller = Principal("a", "https://zulip.example", "a", "synthetic")
    result = await multi_history.multi_channel_history(AsyncMock(), lambda: caller)
    assert search.call_args.args[2] == [{"operator": "channels", "operand": "public"}]
    assert result.data["cursors"] == {"0": None}
    search.reset_mock()
    for kwargs in ({"channels": []}, {"cursors": {"0": None}}):
        result = await multi_history.multi_channel_history(
            AsyncMock(), lambda: caller, **kwargs
        )
        assert result.data["messages"] == []
        assert result.data["exhausted"] is True
    search.assert_not_called()


@pytest.mark.asyncio
async def test_multi_history_revocation_discards_previous_channels(monkeypatch):
    from thinkhome_zulip_mcp import multi_history

    caller = Principal("a", "https://zulip.example", "a", "synthetic")

    async def search(executor, current, narrow, **options):
        nonlocal caller
        current()
        if narrow[0]["operand"] == "ops":
            caller = Principal("b", "https://zulip.example", "b", "synthetic")
        return OperationResult(
            "s",
            data={
                "messages": [{"id": 9}],
                "next_cursor": None,
                "exhausted": True,
                "history_limited": False,
            },
        )

    monkeypatch.setattr(multi_history, "search_messages", search)
    result = await multi_history.multi_channel_history(
        AsyncMock(), lambda: caller, channels=["eng", "ops"]
    )
    assert result.data is None
    assert result.error["code"] == "authentication_required"


@pytest.mark.asyncio
async def test_multi_history_partial_keeps_failed_cursor_for_retry(monkeypatch):
    from thinkhome_zulip_mcp import multi_history

    search = AsyncMock(
        side_effect=[
            OperationResult("s", error={"code": "upstream_error"}),
            OperationResult(
                "s",
                data={
                    "messages": [],
                    "next_cursor": None,
                    "exhausted": True,
                    "history_limited": True,
                },
            ),
        ]
    )
    monkeypatch.setattr(multi_history, "search_messages", search)
    caller = Principal("a", "https://zulip.example", "a", "synthetic")
    result = await multi_history.multi_channel_history(
        AsyncMock(), lambda: caller, channels=["eng", "ops"], cursors={"0": 12}
    )
    assert result.data["cursors"] == {"0": 12, "1": None}
    assert result.data["partial"] is True
    assert result.data["exhausted"] is False
    assert result.data["history_limited"] is True


@pytest.mark.asyncio
async def test_history_formats_context_without_losing_raw_rows_or_cursor(monkeypatch):
    from thinkhome_zulip_mcp import multi_history

    rows = [
        {
            "id": n,
            "sender_id": 10 if n == 2 else 20,
            "timestamp": 100 + n,
            "content": f"message {n}",
            "type": "stream",
            "stream_id": 7,
            "subject": "a" if n < 3 else "b",
            "sender_full_name": "Alice",
            "display_recipient": "eng",
        }
        for n in range(1, 4)
    ]
    monkeypatch.setattr(
        multi_history,
        "search_messages",
        AsyncMock(
            return_value=OperationResult(
                "s",
                data={
                    "messages": rows,
                    "next_cursor": 1,
                    "exhausted": False,
                    "history_limited": False,
                },
            )
        ),
    )
    caller = Principal("a", "https://zulip.example", "a", "synthetic")
    result = await multi_history.multi_channel_history(
        AsyncMock(),
        lambda: caller,
        channels=[7],
        include_formatted=True,
        participant_id=10,
    )
    assert result.error is None
    assert len(result.data["messages"]) == 3
    assert [row["id"] for row in result.data["context_messages"]] == [1, 2]
    assert "message 2" in result.data["formatted"]
    assert "message 3" not in result.data["formatted"]
    assert result.data["cursors"] == {"0": 1}
    assert result.data["context_scope"] == "retrieved_sample_per_topic"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options",
    [
        {"timezone": "invalid-zone"},
        {"include_formatted": 1},
        {"participant_id": True},
        {"participant_id": 0},
        {"participant_id": 10, "sender": "Alice"},
    ],
)
async def test_history_rejects_invalid_presentation_before_io(options):
    from thinkhome_zulip_mcp.multi_history import multi_channel_history

    executor = AsyncMock()
    result = await multi_channel_history(
        executor, lambda: pytest.fail("No IO"), **options
    )
    assert result.error["code"] == "invalid_multi_history_request"
    executor.execute.assert_not_called()

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from thinkhome_zulip_mcp.executor import OperationResult, Principal
from thinkhome_zulip_mcp.search import (
    advanced_search,
    check_matches,
    search_directory,
    search_messages,
    search_topics,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "helper", ["messages", "matches", "directory", "topics", "advanced"]
)
@pytest.mark.parametrize("field", ["email", "api_key"])
async def test_same_identity_rebinding_discards_output(helper, field):
    caller = Principal("same", "https://zulip.example", "alice", "synthetic")

    async def execute(*args):
        nonlocal caller
        caller = replace(caller, **{field: "changed"})
        data = {
            "messages": {} if helper == "matches" else [],
            "members": [],
            "topics": [],
            "found_oldest": True,
            "found_newest": True,
        }
        return OperationResult("test", data=data)

    executor = AsyncMock()
    executor.execute.side_effect = execute

    def current():
        return caller

    if helper == "messages":
        result = await search_messages(executor, current, [])
    elif helper == "matches":
        result = await check_matches(executor, current, [1], [])
    elif helper == "directory":
        result = await search_directory(executor, current, "", kind="users")
    elif helper == "topics":
        result = await search_topics(executor, current, "", [7])
    else:
        result = await advanced_search(executor, current, "", sections=["users"])
    assert result.data is None
    assert result.error["code"] == "authentication_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["email", "api_key"])
async def test_multi_history_discards_results_when_binding_changes_between_channels(
    monkeypatch, field
):
    import thinkhome_zulip_mcp.multi_history as history

    caller = Principal("same", "https://zulip.example", "alice", "synthetic")

    async def fetch(*args, **kwargs):
        nonlocal caller
        caller = replace(caller, **{field: "changed"})
        return OperationResult(
            "test",
            data={
                "messages": [],
                "exhausted": True,
                "next_cursor": None,
                "history_limited": False,
            },
        )

    monkeypatch.setattr(history, "search_messages", fetch)
    result = await history.multi_channel_history(
        AsyncMock(), lambda: caller, channels=[7, 8]
    )
    assert result.data is None
    assert result.error["code"] == "authentication_required"

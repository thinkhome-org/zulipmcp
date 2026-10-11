"""Authorization regressions from the independent channel milestone review."""

import httpx
import pytest

from thinkhome_zulip_mcp.channel_policy import ChannelPolicy
from thinkhome_zulip_mcp.executor import Executor, Principal

CALLER = Principal("a", "https://zulip.example", "a", "synthetic")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "with_term",
    [
        {"operator": "with", "operand": 42},
        {"operator": "with", "operand": 42, "negated": True},
        ["with", "42"],
    ],
)
async def test_with_narrow_cannot_replace_policy_checked_channel(with_term):
    writes = []

    def upstream(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "stream": {"stream_id": 7, "name": "30 Work", "invite_only": False},
                },
            )
        writes.append(request)
        return httpx.Response(200, json={"result": "success"})

    result = await Executor(
        channel_policy=ChannelPolicy(jd_enabled=True),
        transport=httpx.MockTransport(upstream),
    ).execute(
        CALLER,
        "update-message-flags-for-narrow",
        {
            "narrow": [{"operator": "channel", "operand": 7}, with_term],
            "anchor": "oldest",
            "num_before": 0,
            "num_after": 10,
            "flag": "read",
            "op": "add",
        },
    )
    assert writes == []
    assert result.error["code"] == "channel_policy_denied"


@pytest.mark.asyncio
@pytest.mark.parametrize("new_name", ["30 Secret ", "30 Secret\t"])
async def test_rename_checks_the_server_normalized_name(new_name):
    writes = []

    def upstream(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "stream": {"stream_id": 7, "name": "30 Work", "invite_only": False},
                },
            )
        writes.append(request)
        return httpx.Response(200, json={"result": "success"})

    result = await Executor(
        channel_policy=ChannelPolicy(jd_enabled=True, exclude=frozenset({"30 Secret"})),
        transport=httpx.MockTransport(upstream),
    ).execute(CALLER, "update-stream", {"stream_id": 7, "new_name": new_name})
    assert writes == []
    assert result.error["code"] == "channel_policy_denied"


@pytest.mark.asyncio
@pytest.mark.parametrize("helper", ["info", "list"])
@pytest.mark.parametrize("change", ["email", "api_key"])
async def test_rebinding_same_identity_discards_channel_results(helper, change):
    from dataclasses import replace
    from unittest.mock import AsyncMock

    from thinkhome_zulip_mcp.channels import channel_info, list_channels
    from thinkhome_zulip_mcp.executor import OperationResult

    caller = CALLER

    async def execute(*args):
        nonlocal caller
        caller = replace(caller, **{change: "changed"})
        stream = {"stream_id": 7, "name": "30 Work"}
        return OperationResult("test", data={"stream": stream, "streams": [stream]})

    executor = AsyncMock()
    executor.execute.side_effect = execute
    if helper == "info":
        result = await channel_info(
            executor, lambda: caller, stream_id=7, include_subscribers=True
        )
    else:
        result = await list_channels(executor, lambda: caller)
    assert result.data is None
    assert result.error["code"] == "authentication_required"
    assert executor.execute.await_count == 1

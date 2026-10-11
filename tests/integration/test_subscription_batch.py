"""One upstream PATCH for combined subscription changes."""

import json
from urllib.parse import parse_qs

import httpx
import pytest

from thinkhome_zulip_mcp.channel_policy import ChannelPolicy
from thinkhome_zulip_mcp.executor import Executor, Principal
from thinkhome_zulip_mcp.server import create_server


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [True, False])
async def test_subscription_batch_preflights_all_targets_and_uses_one_patch(allowed):
    writes = []

    def upstream(request):
        if request.url.path == "/api/v1/get_stream_id":
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "stream_id": 7 if request.url.params["stream"] == "eng" else 8,
                },
            )
        if request.method == "GET":
            stream_id = int(request.url.path.rsplit("/", 1)[1])
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "stream": {
                        "stream_id": stream_id,
                        "name": "eng" if stream_id == 7 else "old",
                        "invite_only": False,
                    },
                },
            )
        assert request.method == "PATCH"
        assert request.url.path == "/api/v1/users/me/subscriptions"
        body = parse_qs(request.content.decode())
        assert json.loads(body["add"][0]) == [{"name": "eng", "color": "#abc"}]
        assert json.loads(body["delete"][0]) == ["old"]
        writes.append(request)
        return httpx.Response(
            200,
            json={
                "result": "success",
                "subscribed": {"a": ["eng"]},
                "removed": ["old"],
            },
        )

    executor = Executor(
        channel_policy=ChannelPolicy(
            write_allowlist=frozenset({"eng", "old"} if allowed else {"eng"})
        ),
        transport=httpx.MockTransport(upstream),
    )
    server = create_server(
        Principal("a", "https://zulip.example", "a", "synthetic"), executor
    )
    result = await server.call_tool(
        "zulip_update_subscriptions",
        {"add": [{"name": "eng", "color": "#abc"}], "delete": ["old"]},
    )
    assert result.is_error is (not allowed)
    assert len(writes) == int(allowed)


@pytest.mark.asyncio
async def test_subscription_batch_readonly_is_hidden_and_denied_before_io():
    def upstream(request):
        pytest.fail("readonly batch reached upstream")

    caller = Principal("a", "https://zulip.example", "a", "synthetic")
    executor = Executor(
        read_only=True,
        channel_policy=ChannelPolicy(write_allowlist=frozenset({"eng"})),
        transport=httpx.MockTransport(upstream),
    )
    server = create_server(caller, executor)
    assert "zulip_update_subscriptions" not in {
        tool.name for tool in await server.list_tools()
    }
    result = await executor.execute(
        caller, "update-subscriptions", {"add": [{"name": "eng"}]}
    )
    assert result.error["code"] == "read_only_policy"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"add": [], "delete": []},
        {"add": [{"name": "eng"}] * 1000, "delete": ["old"]},
    ],
)
async def test_subscription_batch_empty_or_oversized_does_not_write(arguments):
    def upstream(request):
        pytest.fail("invalid batch reached upstream")

    server = create_server(
        Principal("a", "https://zulip.example", "a", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream)),
    )
    result = await server.call_tool("zulip_update_subscriptions", arguments)
    assert result.is_error


@pytest.mark.asyncio
async def test_subscription_batch_lost_response_is_uncertain_without_retry():
    writes = []

    def upstream(request):
        writes.append(request)
        raise httpx.ReadError("response lost", request=request)

    server = create_server(
        Principal("a", "https://zulip.example", "a", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream)),
    )
    result = await server.call_tool("zulip_update_subscriptions", {"delete": ["old"]})
    assert result.is_error
    assert result.structured_content["uncertain_write"] is True
    assert len(writes) == 1

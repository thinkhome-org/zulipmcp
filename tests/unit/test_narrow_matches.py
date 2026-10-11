import json

import httpx
import pytest

from thinkhome_zulip_mcp.channel_policy import ChannelPolicy
from thinkhome_zulip_mcp.executor import Executor, Principal

CALLER = Principal("alice", "https://zulip.example", "alice", "synthetic")
POLICY = ChannelPolicy.from_environment({"ZULIPCHAT_CHANNEL_FILTER_ENABLED": "true"})


@pytest.mark.asyncio
async def test_narrow_matches_filter_hidden_snippets_through_message_policy():
    def upstream(request):
        if request.url.path.endswith("/matches_narrow"):
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "messages": {
                        "10": {"match_content": "visible", "match_subject": "topic"},
                        "11": {"match_content": "secret", "match_subject": "private"},
                    },
                },
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
        assert request.url.path.endswith("/messages")
        mid = int(request.url.params["anchor"])
        assert json.loads(request.url.params["narrow"]) == [
            {"operator": "id", "operand": mid}
        ]
        return httpx.Response(
            200,
            json={
                "result": "success",
                "messages": [
                    {
                        "id": mid,
                        "type": "stream" if mid == 10 else "private",
                        "stream_id": 7,
                    },
                ],
            },
        )

    executor = Executor(channel_policy=POLICY, transport=httpx.MockTransport(upstream))
    result = await executor.execute(
        CALLER, "check-messages-match-narrow", {"msg_ids": [10, 11], "narrow": []}
    )
    assert not result.error
    assert set(result.data["messages"]) == {"10"}
    assert "secret" not in json.dumps(result.data)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unrequested", "malformed", "lookup"])
async def test_narrow_matches_never_return_partial_snippets_on_invalid_data(failure):
    def upstream(request):
        if request.url.path.endswith("/matches_narrow"):
            key = "99" if failure == "unrequested" else "10"
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "messages": {
                        key: {
                            "match_content": "secret",
                            "match_subject": None
                            if failure == "malformed"
                            else "topic",
                        },
                    },
                },
            )
        if request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "messages": [{"id": 10, "type": "stream", "stream_id": 7}],
                },
            )
        assert failure == "lookup"
        return httpx.Response(
            400, json={"result": "error", "msg": "lookup unavailable"}
        )

    executor = Executor(channel_policy=POLICY, transport=httpx.MockTransport(upstream))
    result = await executor.execute(
        CALLER, "check-messages-match-narrow", {"msg_ids": [10], "narrow": []}
    )
    assert result.error["code"] == (
        "channel_policy_lookup_failed"
        if failure == "lookup"
        else "invalid_upstream_response"
    )
    assert result.data is None

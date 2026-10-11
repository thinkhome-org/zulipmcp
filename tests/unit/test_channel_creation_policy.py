"""Subscribe may create a name only when policy permits every possible visibility."""

import httpx
import pytest

from thinkhome_zulip_mcp.channel_policy import ChannelPolicy
from thinkhome_zulip_mcp.executor import Executor, Principal


@pytest.mark.asyncio
@pytest.mark.parametrize("private_allowed", [True, False])
@pytest.mark.parametrize("postcheck_ok", [True, False])
@pytest.mark.parametrize("operation", ["subscribe", "update-subscriptions"])
async def test_unresolved_name_creation_requires_visibility_independent_policy(
    private_allowed, postcheck_ok, operation
):
    writes = []

    def upstream(request):
        if request.url.path == "/api/v1/get_stream_id":
            if not writes or not postcheck_ok:
                return httpx.Response(
                    400,
                    json={
                        "result": "error",
                        "code": "BAD_REQUEST",
                        "msg": "Invalid channel name",
                    },
                )
            return httpx.Response(200, json={"result": "success", "stream_id": 7})
        if request.url.path == "/api/v1/streams/7":
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "stream": {"stream_id": 7, "name": "eng", "invite_only": False},
                },
            )
        assert request.method == ("POST" if operation == "subscribe" else "PATCH")
        assert request.url.path == "/api/v1/users/me/subscriptions"
        writes.append(request)
        return httpx.Response(
            200, json={"result": "success", "subscribed": {"a": ["eng"]}}
        )

    policy = ChannelPolicy(
        write_allowlist=frozenset({"eng"}),
        private_allowlist=None if private_allowed else frozenset(),
    )
    result = await Executor(
        channel_policy=policy, transport=httpx.MockTransport(upstream)
    ).execute(
        Principal("a", "https://zulip.example", "a", "synthetic"),
        operation,
        {"subscriptions" if operation == "subscribe" else "add": [{"name": "eng"}]},
    )
    assert len(writes) == int(private_allowed)
    if private_allowed and postcheck_ok:
        assert result.error is None
    elif private_allowed:
        assert result.uncertain_write
        assert result.data is None
    else:
        assert result.error["code"] == "channel_policy_denied"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code", [(401, "UNAUTHORIZED"), (403, "BAD_REQUEST"), (400, "OTHER_ERROR")]
)
async def test_lookup_failures_do_not_trigger_creation(status, code):
    def upstream(request):
        assert request.method == "GET"
        return httpx.Response(status, json={"result": "error", "code": code})

    result = await Executor(
        channel_policy=ChannelPolicy(write_allowlist=frozenset({"eng"})),
        transport=httpx.MockTransport(upstream),
    ).execute(
        Principal("a", "https://zulip.example", "a", "synthetic"),
        "subscribe",
        {"subscriptions": [{"name": "eng"}]},
    )
    assert result.error["code"] == "channel_policy_denied"
    assert result.uncertain_write is False

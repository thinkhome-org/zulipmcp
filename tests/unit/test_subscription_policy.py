import httpx
import pytest

from thinkhome_zulip_mcp.channel_policy import ChannelPolicy
from thinkhome_zulip_mcp.executor import Executor, Principal

CALLER = Principal("alice", "https://zulip.example", "alice", "synthetic")
POLICY = ChannelPolicy(jd_enabled=True)


@pytest.mark.asyncio
async def test_subscription_list_filters_channels_without_losing_subscription_fields():
    executor = Executor(
        channel_policy=POLICY,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "result": "success",
                    "subscriptions": [
                        {
                            "stream_id": 7,
                            "name": "30 Work",
                            "invite_only": False,
                            "color": "#fff",
                            "is_muted": True,
                        },
                        {"stream_id": 8, "name": "general", "invite_only": False},
                        {"stream_id": 9, "name": "40 Private", "invite_only": True},
                    ],
                },
            )
        ),
    )
    result = await executor.execute(CALLER, "get-subscriptions", {})
    assert result.error is None
    assert result.data["subscriptions"] == [
        {
            "stream_id": 7,
            "name": "30 Work",
            "invite_only": False,
            "color": "#fff",
            "is_muted": True,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup_error", [False, True])
async def test_other_user_channels_are_filtered_under_acting_identity(lookup_error):
    def upstream(request):
        assert (
            request.headers["authorization"]
            == httpx.BasicAuth("alice", "synthetic")._auth_header
        )
        if request.url.path.endswith("/users/22/channels"):
            return httpx.Response(
                200, json={"result": "success", "subscribed_channel_ids": [7, 8, 9]}
            )
        if lookup_error:
            return httpx.Response(503, json={"result": "error"})
        stream_id = int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(
            200,
            json={
                "result": "success",
                "stream": {
                    "stream_id": stream_id,
                    "name": "general" if stream_id == 8 else "30 Work",
                    "invite_only": stream_id == 9,
                },
            },
        )

    executor = Executor(channel_policy=POLICY, transport=httpx.MockTransport(upstream))
    result = await executor.execute(CALLER, "get-user-channels", {"user_id": 22})
    if lookup_error:
        assert result.error["code"] == "channel_policy_lookup_failed"
        assert result.data is None
    else:
        assert result.error is None
        assert result.data == {"result": "success", "subscribed_channel_ids": [7]}


@pytest.mark.asyncio
@pytest.mark.parametrize("ids", [[True], [7, 7], [-1], "invalid"])
async def test_invalid_channel_id_list_is_not_returned(ids):
    executor = Executor(
        channel_policy=POLICY,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"result": "success", "subscribed_channel_ids": ids}
            )
        ),
    )
    result = await executor.execute(CALLER, "get-user-channels", {"user_id": 22})
    assert result.error["code"] == "invalid_upstream_response"
    assert result.data is None

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


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [True, False])
async def test_subscribe_validates_all_existing_channels_before_write(allowed):
    calls = []

    def upstream(request):
        calls.append(request)
        if request.url.path.endswith("/get_stream_id"):
            return httpx.Response(200, json={"result": "success", "stream_id": 7})
        if request.method == "GET":
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
        assert request.method == "POST"
        return httpx.Response(
            200, json={"result": "success", "subscribed": {"alice": ["30 Work"]}}
        )

    executor = Executor(
        channel_policy=ChannelPolicy.from_environment(
            {
                "BOT_ALLOWED_WRITE_STREAMS": "30 Work" if allowed else "Other",
            }
        ),
        transport=httpx.MockTransport(upstream),
    )
    result = await executor.execute(
        CALLER, "subscribe", {"subscriptions": [{"name": "30 Work"}]}
    )
    assert (result.error is None) == allowed
    assert sum(r.method == "POST" for r in calls) == int(allowed)
    if not allowed:
        assert result.error["code"] == "channel_policy_denied"


@pytest.mark.asyncio
async def test_subscribe_missing_channel_does_not_create_under_policy():
    def upstream(request):
        assert request.method == "GET"
        return httpx.Response(400, json={"result": "error", "msg": "Unknown channel"})

    result = await Executor(
        channel_policy=POLICY, transport=httpx.MockTransport(upstream)
    ).execute(CALLER, "subscribe", {"subscriptions": [{"name": "30 Missing"}]})
    assert result.error["code"] == "channel_policy_denied"


@pytest.mark.asyncio
async def test_subscribe_read_only_fails_before_lookup():
    def upstream(request):
        pytest.fail("read-only subscribe reached upstream")

    result = await Executor(
        read_only=True, channel_policy=POLICY, transport=httpx.MockTransport(upstream)
    ).execute(
        CALLER,
        "subscribe",
        {
            "subscriptions": [{"name": "30 Work"}],
        },
    )
    assert result.error["code"] == "read_only_policy"


@pytest.mark.asyncio
async def test_subscribe_reassigned_name_reports_uncertain_write_without_retry():
    writes = 0

    def upstream(request):
        nonlocal writes
        if request.method == "POST":
            writes += 1
            return httpx.Response(200, json={"result": "success"})
        channel_id = 8 if writes else 7
        if request.url.path.endswith("/get_stream_id"):
            return httpx.Response(
                200, json={"result": "success", "stream_id": channel_id}
            )
        return httpx.Response(
            200,
            json={
                "result": "success",
                "stream": {
                    "stream_id": channel_id,
                    "name": "30 Work",
                    "invite_only": False,
                },
            },
        )

    result = await Executor(
        channel_policy=POLICY, transport=httpx.MockTransport(upstream)
    ).execute(CALLER, "subscribe", {"subscriptions": [{"name": "30 Work"}]})
    assert result.error["code"] == "subscription_target_changed"
    assert result.uncertain_write
    assert result.data is None
    assert writes == 1

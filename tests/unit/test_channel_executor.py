import httpx
import pytest

from thinkhome_zulip_mcp.channel_policy import ChannelPolicy
from thinkhome_zulip_mcp.executor import Executor, Principal

CALLER = Principal("alice", "https://zulip.example", "alice", "synthetic")
POLICY = ChannelPolicy.from_environment({"ZULIPCHAT_CHANNEL_FILTER_ENABLED": "true"})


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [7, "30 Work"])
async def test_send_resolves_metadata_and_uses_checked_id(target):
    calls = []

    def upstream(request):
        calls.append(request)
        assert (
            request.headers["authorization"]
            == httpx.BasicAuth("alice", "synthetic")._auth_header
        )
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
        assert b"to=7" in request.content
        return httpx.Response(200, json={"result": "success", "id": 1})

    executor = Executor(channel_policy=POLICY, transport=httpx.MockTransport(upstream))
    result = await executor.execute(
        CALLER,
        "send-message",
        {
            "type": "stream",
            "to": target,
            "topic": "test",
            "content": "test",
        },
    )
    assert result.error is None
    assert sum(r.method == "POST" for r in calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {"stream_id": 7, "name": "general", "invite_only": False},
        {"stream_id": 7, "name": "30 Private", "invite_only": True},
        {"stream_id": 7, "name": "30 Unknown"},
        {"stream_id": 8, "name": "30 Wrong", "invite_only": False},
    ],
)
async def test_send_denies_blocked_or_invalid_metadata(metadata):
    def upstream(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"result": "success", "stream": metadata})

    executor = Executor(channel_policy=POLICY, transport=httpx.MockTransport(upstream))
    result = await executor.execute(
        CALLER,
        "send-message",
        {
            "type": "stream",
            "to": 7,
            "topic": "test",
            "content": "test",
        },
    )
    assert result.error["code"] == "channel_policy_denied"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,arguments",
    [
        ("send-message", {"type": "direct", "to": [1], "content": "test"}),
        ("get-drafts", {}),
        ("register-queue", {}),
    ],
)
async def test_unguarded_paths_fail_closed(operation, arguments):
    def upstream(request):
        pytest.fail("Blocked operation reached upstream")

    executor = Executor(channel_policy=POLICY, transport=httpx.MockTransport(upstream))
    result = await executor.execute(CALLER, operation, arguments)
    assert result.error["code"] in {
        "channel_policy_denied",
        "channel_policy_not_supported",
    }


@pytest.mark.asyncio
async def test_channel_listing_filters_per_caller_without_cross_identity_cache():
    seen = []

    def upstream(request):
        seen.append(request.headers["authorization"])
        private = (
            request.headers["authorization"]
            == httpx.BasicAuth("bob", "synthetic")._auth_header
        )
        return httpx.Response(
            200,
            json={
                "result": "success",
                "streams": [
                    {"stream_id": 7, "name": "30 Work", "invite_only": private},
                    {"stream_id": 8, "name": "general", "invite_only": False},
                ],
            },
        )

    executor = Executor(channel_policy=POLICY, transport=httpx.MockTransport(upstream))
    alice = await executor.execute(CALLER, "get-streams", {})
    bob = await executor.execute(
        Principal("bob", "https://zulip.example", "bob", "synthetic"), "get-streams", {}
    )
    assert alice.error is None
    assert [s["stream_id"] for s in alice.data["streams"]] == [7]
    assert bob.error is None
    assert bob.data["streams"] == []
    assert len(set(seen)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"result": "success", "streams": [{"stream_id": 7, "name": "30 Work"}]},
        {"result": "success", "streams": "invalid"},
        {"result": "error", "msg": "denied", "streams": [{"name": "secret"}]},
    ],
)
async def test_channel_listing_never_returns_unvalidated_payload(payload):
    executor = Executor(
        channel_policy=POLICY,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload)
        ),
    )
    result = await executor.execute(CALLER, "get-streams", {})
    assert result.error is not None
    assert result.data is None
    assert result.error["code"] in {"invalid_upstream_response", "upstream_error"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,args",
    [
        ("get-stream-id", {"stream": "30 Work"}),
        ("get-stream-by-id", {"stream_id": 7}),
    ],
)
@pytest.mark.parametrize("private", [False, True])
async def test_direct_channel_lookup_enforces_policy_in_read_only(
    operation, args, private
):
    def upstream(request):
        if request.url.path.endswith("/get_stream_id"):
            return httpx.Response(200, json={"result": "success", "stream_id": 7})
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "result": "success",
                "stream": {
                    "stream_id": 7,
                    "name": "30 Work",
                    "invite_only": private,
                },
            },
        )

    executor = Executor(
        channel_policy=POLICY, read_only=True, transport=httpx.MockTransport(upstream)
    )
    result = await executor.execute(CALLER, operation, args)
    if private:
        assert result.error["code"] == "channel_policy_denied"
        assert result.data is None
    else:
        assert result.error is None
        assert (result.data.get("stream") or result.data)["stream_id"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,private,allowed",
    [
        ("stream", False, True),
        ("stream", True, False),
        ("private", False, False),
        ("unknown", False, False),
    ],
)
async def test_message_read_checks_current_channel_or_dm_rule(kind, private, allowed):
    calls = []

    def upstream(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/messages/10"):
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "message": {
                        "id": 10,
                        "type": kind,
                        "stream_id": 7,
                        "display_recipient": "30 Public",
                        "content": "sensitive",
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "result": "success",
                "stream": {
                    "stream_id": 7,
                    "name": "30 Current",
                    "invite_only": private,
                },
            },
        )

    executor = Executor(channel_policy=POLICY, transport=httpx.MockTransport(upstream))
    result = await executor.execute(CALLER, "get-message", {"message_id": 10})
    if allowed:
        assert result.error is None
        assert result.data["message"]["content"] == "sensitive"
    else:
        assert result.error["code"] == "channel_policy_denied"
        assert result.data is None
    assert len(calls) == (2 if kind == "stream" else 1)


@pytest.mark.asyncio
async def test_message_read_rejects_mismatched_message_id():
    executor = Executor(
        channel_policy=POLICY,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "result": "success",
                    "message": {
                        "id": 11,
                        "type": "private",
                        "content": "wrong message",
                    },
                },
            )
        ),
    )
    result = await executor.execute(CALLER, "get-message", {"message_id": 10})
    assert result.error["code"] == "invalid_upstream_response"
    assert result.data is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,extra",
    [
        ("update-message", {"content": "updated"}),
        ("delete-message", {}),
        ("add-reaction", {"emoji_name": "smile"}),
        ("remove-reaction", {"emoji_name": "smile"}),
    ],
)
@pytest.mark.parametrize("write_allowed", [True, False])
async def test_message_mutations_require_source_write_permission(
    operation, extra, write_allowed
):
    writes = []
    policy = ChannelPolicy.from_environment(
        {
            "BOT_ALLOWED_WRITE_STREAMS": "30 Work" if write_allowed else "other",
        }
    )

    def upstream(request):
        if request.method != "GET":
            writes.append(request)
            return httpx.Response(200, json={"result": "success"})
        if request.url.path.endswith("/messages/10"):
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "message": {
                        "id": 10,
                        "type": "stream",
                        "stream_id": 7,
                    },
                },
            )
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

    executor = Executor(channel_policy=policy, transport=httpx.MockTransport(upstream))
    result = await executor.execute(CALLER, operation, {"message_id": 10, **extra})
    assert (result.error is None) == write_allowed
    assert len(writes) == int(write_allowed)
    if not write_allowed:
        assert result.error["code"] == "channel_policy_denied"


@pytest.mark.asyncio
async def test_message_move_checks_destination_write_permission():
    def upstream(request):
        assert request.method == "GET"
        if request.url.path.endswith("/messages/10"):
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "message": {
                        "id": 10,
                        "type": "stream",
                        "stream_id": 7,
                    },
                },
            )
        stream_id = int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(
            200,
            json={
                "result": "success",
                "stream": {
                    "stream_id": stream_id,
                    "name": "30 Work" if stream_id == 7 else "40 Other",
                    "invite_only": False,
                },
            },
        )

    executor = Executor(
        channel_policy=ChannelPolicy.from_environment(
            {
                "BOT_ALLOWED_WRITE_STREAMS": "30 Work",
            }
        ),
        transport=httpx.MockTransport(upstream),
    )
    result = await executor.execute(
        CALLER, "update-message", {"message_id": 10, "stream_id": 8}
    )
    assert result.error["code"] == "channel_policy_denied"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,extra,path",
    [
        ("get-stream-topics", {}, "/users/me/7/topics"),
        ("get-subscribers", {}, "/streams/7/members"),
        ("get-subscription-status", {"user_id": 42}, "/users/42/subscriptions/7"),
    ],
)
@pytest.mark.parametrize("private", [False, True])
async def test_channel_scoped_reads_check_metadata_before_fetch(
    operation, extra, path, private
):
    calls = []

    def upstream(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/streams/7"):
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "stream": {
                        "stream_id": 7,
                        "name": "30 Work",
                        "invite_only": private,
                    },
                },
            )
        assert not private
        assert request.url.path == "/api/v1" + path
        return httpx.Response(200, json={"result": "success"})

    executor = Executor(
        channel_policy=POLICY, read_only=True, transport=httpx.MockTransport(upstream)
    )
    result = await executor.execute(CALLER, operation, {"stream_id": 7, **extra})
    assert (result.error is None) == (not private)
    assert len(calls) == (1 if private else 2)
    if private:
        assert result.error["code"] == "channel_policy_denied"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["stream", "channel"])
@pytest.mark.parametrize("allowed", [True, False])
async def test_typing_checks_channel_write_access(kind, allowed):
    calls = []
    policy = ChannelPolicy.from_environment(
        {"BOT_ALLOWED_WRITE_STREAMS": "30 Work" if allowed else "Elsewhere"}
    )

    def upstream(request):
        calls.append(request)
        if request.method == "GET":
            assert request.url.path.endswith("/streams/7")
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
        assert request.url.path.endswith("/typing")
        assert b"stream_id=7" in request.content
        return httpx.Response(200, json={"result": "success"})

    executor = Executor(channel_policy=policy, transport=httpx.MockTransport(upstream))
    result = await executor.execute(
        CALLER,
        "set-typing-status",
        {
            "type": kind,
            "stream_id": 7,
            "topic": "",
            "op": "start",
        },
    )
    if allowed:
        assert result.error is None
        assert len(calls) == 2
    else:
        assert result.error["code"] == "channel_policy_denied"
        assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [True, False])
@pytest.mark.parametrize("allowed", [True, False])
async def test_typing_direct_default_respects_dm_policy(explicit, allowed):
    calls = []
    policy = ChannelPolicy.from_environment(
        {
            "ZULIPCHAT_CHANNEL_FILTER_ENABLED": "true",
            "ZULIPCHAT_EXCLUDE_DMS": "false" if allowed else "true",
        }
    )

    def upstream(request):
        calls.append(request)
        assert request.method == "POST"
        return httpx.Response(200, json={"result": "success"})

    arguments = {"to": [2], "op": "stop"}
    if explicit:
        arguments["type"] = "direct"
    result = await Executor(
        channel_policy=policy, transport=httpx.MockTransport(upstream)
    ).execute(CALLER, "set-typing-status", arguments)
    assert (result.error is None) == allowed
    assert len(calls) == int(allowed)
    if not allowed:
        assert result.error["code"] == "channel_policy_denied"


@pytest.mark.asyncio
async def test_typing_read_only_rejects_before_metadata_lookup():
    def upstream(request):
        pytest.fail("read-only typing reached upstream")

    result = await Executor(
        read_only=True, channel_policy=POLICY, transport=httpx.MockTransport(upstream)
    ).execute(
        CALLER,
        "set-typing-status",
        {
            "type": "stream",
            "stream_id": 7,
            "topic": "x",
            "op": "start",
        },
    )
    assert result.error["code"] == "read_only_policy"


@pytest.mark.asyncio
async def test_custom_emoji_metadata_available_with_channel_policy_and_read_only():
    def upstream(request):
        assert request.method == "GET"
        assert request.url.path.endswith("/realm/emoji")
        assert (
            request.headers["authorization"]
            == httpx.BasicAuth("alice", "synthetic")._auth_header
        )
        return httpx.Response(
            200,
            json={
                "result": "success",
                "emoji": {
                    "1": {"name": "robot_ear", "deactivated": False, "id": "1"},
                },
            },
        )

    result = await Executor(
        channel_policy=POLICY, read_only=True, transport=httpx.MockTransport(upstream)
    ).execute(CALLER, "get-custom-emoji", {})
    assert result.error is None
    assert result.data["emoji"]["1"]["name"] == "robot_ear"

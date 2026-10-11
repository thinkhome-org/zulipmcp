import json
from urllib.parse import parse_qs

import httpx
import pytest

from thinkhome_zulip_mcp.channel_policy import ChannelPolicy
from thinkhome_zulip_mcp.events import EventQueues
from thinkhome_zulip_mcp.executor import Executor, Principal

CALLER = Principal("alice", "https://zulip.example", "alice", "synthetic")
POLICY = ChannelPolicy.from_environment({"ZULIPCHAT_CHANNEL_FILTER_ENABLED": "true"})


@pytest.mark.asyncio
async def test_filtered_registration_preserves_control_metadata_only_and_can_delete():
    calls = []

    def upstream(request):
        calls.append(request)
        if request.method == "DELETE":
            return httpx.Response(200, json={"result": "success"})
        assert request.method == "POST"
        assert json.loads(parse_qs(request.content.decode())["event_types"][0]) == [
            "message"
        ]
        return httpx.Response(
            200,
            json={
                "result": "success",
                "queue_id": "q",
                "last_event_id": -1,
                "max_message_id": 42,
                "zulip_feature_level": 445,
                "streams": [{"name": "secret"}],
                "unread_msgs": {"secret": True},
            },
        )

    queues = EventQueues(
        Executor(channel_policy=POLICY, transport=httpx.MockTransport(upstream))
    )
    registered = await queues.register(CALLER, {"event_types": ["message"]})
    assert registered.error is None
    assert registered.data["max_message_id"] == 42
    assert "streams" not in registered.data
    assert "unread_msgs" not in registered.data
    assert "queue_id" not in registered.data
    assert (await queues.delete(CALLER, registered.data["handle"])).error is None
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"event_types": ["message", "stream"]},
        {"event_types": ["message"], "fetch_event_types": ["subscription"]},
    ],
)
async def test_unsupported_event_registration_fails_before_network(arguments):
    def upstream(request):
        pytest.fail("Unsupported event types reached upstream")

    executor = Executor(channel_policy=POLICY, transport=httpx.MockTransport(upstream))
    result = await executor.execute(CALLER, "register-queue", arguments)
    assert result.error["code"] == "channel_policy_not_supported"


@pytest.mark.asyncio
async def test_read_only_still_denies_explicit_message_queue():
    def upstream(request):
        pytest.fail("Read-only registration reached upstream")

    executor = Executor(
        channel_policy=POLICY, read_only=True, transport=httpx.MockTransport(upstream)
    )
    result = await executor.execute(
        CALLER, "register-queue", {"event_types": ["message"]}
    )
    assert result.error["code"] == "read_only_policy"


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_status", [200, 503])
async def test_invalid_registration_cursor_cleans_up_known_queue(cleanup_status):
    calls = []

    def upstream(request):
        calls.append(request.method)
        if request.method == "DELETE":
            return httpx.Response(
                cleanup_status,
                json={"result": "success" if cleanup_status == 200 else "error"},
            )
        return httpx.Response(
            200, json={"result": "success", "queue_id": "q", "last_event_id": True}
        )

    executor = Executor(channel_policy=POLICY, transport=httpx.MockTransport(upstream))
    result = await executor.execute(
        CALLER, "register-queue", {"event_types": ["message"]}
    )
    assert result.error["code"] == "invalid_queue_response"
    assert result.data is None
    assert result.uncertain_write == (cleanup_status != 200)
    assert calls == ["POST", "DELETE"]


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_filtered_poll_keeps_raw_cursor_and_explicit_replay(private):
    acknowledgements = []

    def upstream(request):
        if request.method == "POST":
            return httpx.Response(
                200, json={"result": "success", "queue_id": "q", "last_event_id": -1}
            )
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
        acknowledgements.append(request.url.params["last_event_id"])
        return httpx.Response(
            200,
            json={
                "result": "success",
                "events": [
                    {
                        "id": 1,
                        "type": "message",
                        "message": {
                            "id": 10,
                            "type": "stream",
                            "stream_id": 7,
                            "content": "visible only if public",
                        },
                    },
                    {
                        "id": 2,
                        "type": "message",
                        "message": {"id": 11, "type": "private", "content": "hidden"},
                    },
                ],
            },
        )

    queues = EventQueues(
        Executor(channel_policy=POLICY, transport=httpx.MockTransport(upstream))
    )
    handle = (await queues.register(CALLER, {"event_types": ["message"]})).data[
        "handle"
    ]
    for _ in range(2):
        result = await queues.poll(CALLER, handle, -1, wait_seconds=0)
        assert result.error is None
        assert [e["id"] for e in result.data["events"]] == ([] if private else [1])
        assert result.data["next_event_id"] == 2
    assert acknowledgements == ["-1", "-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "events",
    [
        [{"id": -2, "type": "heartbeat"}],
        [{"id": True, "type": "heartbeat"}],
        [{"id": 1, "type": "heartbeat"}, {"id": 1, "type": "heartbeat"}],
        [{"id": 2, "type": "heartbeat"}, {"id": 1, "type": "heartbeat"}],
        [{"id": 1, "type": "message", "message": {"id": 0}}],
    ],
)
async def test_invalid_event_batch_returns_no_content_or_cursor(events):
    executor = Executor(
        channel_policy=POLICY,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"result": "success", "events": events}
            )
        ),
    )
    result = await executor.execute(
        CALLER, "get-events", {"queue_id": "q", "last_event_id": -3, "dont_block": True}
    )
    assert result.error["code"] == "invalid_events_response"
    assert result.data is None


@pytest.mark.asyncio
async def test_unknown_event_type_does_not_return_partial_batch():
    executor = Executor(
        channel_policy=POLICY,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "result": "success",
                    "events": [
                        {"id": 1, "type": "heartbeat"},
                        {"id": 2, "type": "stream", "streams": [{"name": "secret"}]},
                    ],
                },
            )
        ),
    )
    result = await executor.execute(
        CALLER, "get-events", {"queue_id": "q", "last_event_id": -1}
    )
    assert result.error["code"] == "channel_policy_event_not_supported"
    assert result.data is None


@pytest.mark.asyncio
async def test_filtered_queue_expiry_preserves_resync_without_payload_leak():
    def upstream(request):
        if request.method == "POST":
            return httpx.Response(
                200, json={"result": "success", "queue_id": "q", "last_event_id": -1}
            )
        return httpx.Response(
            400,
            json={
                "result": "error",
                "code": "BAD_EVENT_QUEUE_ID",
                "unexpected": "secret",
            },
        )

    queues = EventQueues(
        Executor(channel_policy=POLICY, transport=httpx.MockTransport(upstream))
    )
    handle = (await queues.register(CALLER, {"event_types": ["message"]})).data[
        "handle"
    ]
    result = await queues.poll(CALLER, handle, -1, wait_seconds=0)
    assert result.error["code"] == "queue_expired"
    assert result.data == {"resync_required": True}
    assert (await queues.poll(CALLER, handle, -1)).error[
        "code"
    ] == "invalid_queue_handle"


@pytest.mark.asyncio
async def test_event_metadata_failure_returns_no_partial_events():
    def upstream(request):
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "events": [
                        {"id": 1, "type": "heartbeat"},
                        {
                            "id": 2,
                            "type": "message",
                            "message": {"id": 10, "type": "stream", "stream_id": 7},
                        },
                    ],
                },
            )
        return httpx.Response(503, json={"result": "error", "unexpected": "secret"})

    executor = Executor(channel_policy=POLICY, transport=httpx.MockTransport(upstream))
    result = await executor.execute(
        CALLER, "get-events", {"queue_id": "q", "last_event_id": -1}
    )
    assert result.error["code"] == "channel_policy_lookup_failed"
    assert result.data is None


@pytest.mark.asyncio
async def test_event_filter_deadline_returns_no_cursor(monkeypatch):
    import asyncio

    original_timeout = asyncio.timeout

    def immediate_timeout(seconds):
        assert seconds == 30
        return original_timeout(0)

    async def upstream(request):
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "events": [
                        {
                            "id": 1,
                            "type": "message",
                            "message": {"id": 10, "type": "stream", "stream_id": 7},
                        },
                    ],
                },
            )
        await asyncio.sleep(0)
        pytest.fail("Metadata request should have been cancelled")

    monkeypatch.setattr(asyncio, "timeout", immediate_timeout)
    executor = Executor(channel_policy=POLICY, transport=httpx.MockTransport(upstream))
    result = await executor.execute(
        CALLER, "get-events", {"queue_id": "q", "last_event_id": -1}
    )
    assert result.error["code"] == "channel_policy_lookup_timeout"
    assert result.data is None


@pytest.mark.asyncio
async def test_reaction_registration_retains_only_control_metadata():
    def upstream(request):
        return httpx.Response(
            200,
            json={
                "result": "success",
                "queue_id": "q",
                "last_event_id": -1,
                "messages": [{"content": "must not escape"}],
            },
        )

    result = await Executor(
        channel_policy=POLICY, transport=httpx.MockTransport(upstream)
    ).execute(
        CALLER,
        "register-queue",
        {"event_types": ["message", "reaction"], "fetch_event_types": []},
    )
    assert result.error is None
    assert result.data == {"result": "success", "queue_id": "q", "last_event_id": -1}


@pytest.mark.asyncio
@pytest.mark.parametrize("visible", [True, False])
async def test_reaction_events_check_message_channel_and_preserve_ack(visible):
    calls = []
    event = {
        "id": 3,
        "type": "reaction",
        "op": "add",
        "message_id": 17,
        "user_id": 2,
        "emoji_name": "stop_sign",
        "emoji_code": "1f6d1",
        "reaction_type": "unicode_emoji",
    }

    def upstream(request):
        calls.append(request)
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json={"result": "success", "events": [event]})
        if request.url.path.endswith("/messages/17"):
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "message": {
                        "id": 17,
                        "type": "stream",
                        "stream_id": 7,
                    },
                },
            )
        assert request.url.path.endswith("/streams/7")
        return httpx.Response(
            200,
            json={
                "result": "success",
                "stream": {
                    "stream_id": 7,
                    "name": "30 Work" if visible else "Hidden",
                    "invite_only": False,
                },
            },
        )

    result = await Executor(
        channel_policy=POLICY, transport=httpx.MockTransport(upstream)
    ).execute(
        CALLER, "get-events", {"queue_id": "q", "last_event_id": 2, "dont_block": True}
    )
    assert result.error is None
    assert result.data["events"] == ([event] if visible else [])
    assert result.data["policy_event_cursor"] == 3
    assert len(calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"message_id": True}, {"op": []}, {"user_id": 0}])
async def test_invalid_reaction_event_fails_before_message_lookup(changes):
    def upstream(request):
        assert request.url.path.endswith("/events")
        return httpx.Response(
            200,
            json={
                "result": "success",
                "events": [
                    {
                        "id": 1,
                        "type": "reaction",
                        "op": "add",
                        "message_id": 17,
                        "user_id": 2,
                        "emoji_name": "stop_sign",
                        **changes,
                    }
                ],
            },
        )

    result = await Executor(
        channel_policy=POLICY, transport=httpx.MockTransport(upstream)
    ).execute(
        CALLER, "get-events", {"queue_id": "q", "last_event_id": -1, "dont_block": True}
    )
    assert result.error["code"] == "invalid_events_response"
    assert result.data is None


@pytest.mark.asyncio
async def test_reaction_channel_lookup_failure_does_not_acknowledge_batch():
    def upstream(request):
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "events": [
                        {
                            "id": 3,
                            "type": "reaction",
                            "op": "add",
                            "message_id": 17,
                            "user_id": 2,
                            "emoji_name": "stop_sign",
                        }
                    ],
                },
            )
        if request.url.path.endswith("/messages/17"):
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "message": {
                        "id": 17,
                        "type": "stream",
                        "stream_id": 7,
                    },
                },
            )
        return httpx.Response(403, json={"result": "error", "msg": "Forbidden"})

    result = await Executor(
        channel_policy=POLICY, transport=httpx.MockTransport(upstream)
    ).execute(
        CALLER, "get-events", {"queue_id": "q", "last_event_id": 2, "dont_block": True}
    )
    assert result.error["code"] == "channel_policy_lookup_failed"
    assert result.data is None

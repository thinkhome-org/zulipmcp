import asyncio

import pytest

from thinkhome_zulip_mcp.channel_policy import ChannelPolicy
from thinkhome_zulip_mcp.conversations import Conversations
from thinkhome_zulip_mcp.executor import OperationResult, Principal

ALICE = Principal("alice", "https://zulip.example", "alice", "synthetic")
BOB = Principal("bob", "https://zulip.example", "bob", "synthetic")


def message(mid, content="hello", sender=2, **changes):
    return {
        "id": mid,
        "type": "stream",
        "stream_id": 7,
        "subject": "topic",
        "sender_id": sender,
        "content": content,
        "reactions": [],
        **changes,
    }


class Upstream:
    read_only = False
    channel_policy = ChannelPolicy()

    def __init__(self):
        self.calls = []
        self.messages = [message(10)]
        self.last_message = message(20, sender=1)
        self.send_error = False
        self.more = False

    async def execute(self, principal, operation, arguments):
        self.calls.append((principal, operation, arguments))
        data = {"result": "success"}
        if operation == "get-own-user":
            data |= {"user_id": 1, "max_message_id": 10}
        elif operation == "get-stream-id":
            data |= {"stream_id": 7}
        elif operation == "get-stream-by-id":
            data |= {"stream": {"stream_id": 7, "name": "work", "invite_only": False}}
        elif operation == "get-messages":
            data |= {
                "messages": self.messages,
                "found_oldest": True,
                "found_newest": not self.more,
                "history_limited": False,
            }
        elif operation == "get-subscription-status":
            data |= {"is_subscribed": True}
        elif operation == "get-message":
            data |= {"message": self.last_message}
        elif operation == "send-message":
            if self.send_error:
                return OperationResult(
                    "test",
                    error={"code": "upstream_transport_error"},
                    uncertain_write=True,
                )
            data |= {"id": 20}
        return OperationResult("test", data=data)


@pytest.mark.asyncio
async def test_context_is_owned_and_failed_reply_does_not_advance_cursor():
    upstream = Upstream()
    manager = Conversations(upstream)
    created = await manager.set_context(lambda: ALICE, "work", "topic")
    handle = created.data["handle"]
    before = len(upstream.calls)
    assert (await manager.reply(lambda: BOB, handle, "no")).error[
        "code"
    ] == "invalid_conversation_handle"
    assert len(upstream.calls) == before
    upstream.messages = [message(11)]
    upstream.send_error = True
    failed = await manager.reply(lambda: ALICE, handle, "response")
    assert failed.uncertain_write
    upstream.send_error = False
    result = await manager.reply(lambda: ALICE, handle, "response")
    assert result.data["missed_messages"][0]["id"] == 11
    anchors = [args["anchor"] for _, op, args in upstream.calls if op == "get-messages"]
    assert anchors[-2:] == ["10", "10"]
    assert result.data["sent_message_id"] == 20
    upstream.messages = [message(12)]  # Arrived between catchup and send; must survive.
    result = await manager.reply(lambda: ALICE, handle, "next")
    assert result.data["missed_messages"][0]["id"] == 12


@pytest.mark.asyncio
async def test_dismissal_prevents_reply_and_silent_end_stops_typing():
    upstream = Upstream()
    manager = Conversations(upstream, auto_typing_start=False)
    handle = (await manager.set_context(lambda: ALICE, 7, "topic")).data["handle"]
    upstream.messages = []
    assert not (await manager.reply(lambda: ALICE, handle, "hello")).error
    upstream.last_message["reactions"] = [{"emoji_name": "stop_sign", "user_id": 2}]
    result = await manager.reply(lambda: ALICE, handle, "must not send")
    assert result.error["code"] == "session_dismissed"
    assert len([op for _, op, _ in upstream.calls if op == "send-message"]) == 1
    ended = await manager.end(lambda: ALICE, handle, "")
    assert ended.data["ended"]
    assert upstream.calls[-1][1:] == (
        "set-typing-status",
        {"type": "stream", "stream_id": 7, "topic": "topic", "op": "stop"},
    )


@pytest.mark.asyncio
async def test_reply_returns_catchup_pages_without_sending_until_exhausted():
    upstream = Upstream()
    manager = Conversations(upstream)
    handle = (await manager.set_context(lambda: ALICE, 7, "topic")).data["handle"]
    upstream.messages, upstream.more = (
        [message(11, "/nobots hidden"), message(12)],
        True,
    )
    result = await manager.reply(lambda: ALICE, handle, "reply")
    assert result.error["code"] == "catchup_required"
    assert [m["id"] for m in result.data["missed_messages"]] == [12]
    assert not any(op == "send-message" for _, op, _ in upstream.calls)
    upstream.messages, upstream.more = [message(13)], False
    assert not (await manager.reply(lambda: ALICE, handle, "reply")).error


@pytest.mark.asyncio
async def test_empty_topic_prefix_budget_and_hook_failure_after_send():
    upstream = Upstream()
    upstream.messages = [message(10, subject="")]
    manager = Conversations(
        upstream, prefix="prefix:", message_max_length=12, on_reply=lambda *_: 1 / 0
    )
    handle = (await manager.set_context(lambda: ALICE, 7, "")).data["handle"]
    before = len(upstream.calls)
    assert (await manager.reply(lambda: ALICE, handle, "too long")).error[
        "code"
    ] == "invalid_message"
    assert len(upstream.calls) == before
    upstream.messages = []
    sent = await manager.reply(lambda: ALICE, handle, "ok")
    assert not sent.error
    assert "on_reply_failed" in sent.data["warnings"]
    assert (
        next(args for _, op, args in upstream.calls if op == "send-message")["content"]
        == "prefix:ok"
    )


@pytest.mark.asyncio
async def test_capacity_and_close_cleanup():
    upstream = Upstream()
    manager = Conversations(upstream, max_sessions=1)
    assert not (await manager.set_context(lambda: ALICE, 7, "topic")).error
    assert (await manager.set_context(lambda: BOB, 7, "topic")).error[
        "code"
    ] == "conversation_limit_exceeded"
    await manager.close()
    assert (await manager.set_context(lambda: ALICE, 7, "topic")).error[
        "code"
    ] == "conversation_manager_closed"


@pytest.mark.asyncio
async def test_concurrent_reply_is_rejected_instead_of_duplicate_send():
    upstream = Upstream()
    manager = Conversations(upstream)
    handle = (await manager.set_context(lambda: ALICE, 7, "topic")).data["handle"]
    original = upstream.execute
    started, release = asyncio.Event(), asyncio.Event()

    async def paused(principal, operation, arguments):
        if operation == "send-message":
            started.set()
            await release.wait()
        return await original(principal, operation, arguments)

    upstream.execute = paused
    upstream.messages = []
    first = asyncio.create_task(manager.reply(lambda: ALICE, handle, "one"))
    await asyncio.wait_for(started.wait(), 1)
    try:
        assert (await manager.reply(lambda: ALICE, handle, "two")).error[
            "code"
        ] == "conversation_busy"
    finally:
        release.set()
        await first


@pytest.mark.asyncio
async def test_setup_cancellation_does_not_leak_an_unreturned_handle():
    upstream = Upstream()
    entered = asyncio.Event()

    async def hook(*_):
        entered.set()
        await asyncio.Event().wait()

    manager = Conversations(upstream, on_set_context=hook)
    task = asyncio.create_task(manager.set_context(lambda: ALICE, 7, "topic"))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not manager._sessions
    assert manager._pending == 0


@pytest.mark.asyncio
async def test_identity_change_during_setup_never_reads_under_another_actor():
    upstream = Upstream()
    original = upstream.execute
    actor = ALICE

    async def switched(principal, operation, arguments):
        nonlocal actor
        result = await original(principal, operation, arguments)
        if operation == "get-stream-by-id":
            actor = BOB
        return result

    upstream.execute = switched
    manager = Conversations(upstream)
    result = await manager.set_context(lambda: actor, 7, "topic")
    assert result.error["code"] == "authentication_required"
    assert all(principal == ALICE for principal, _, _ in upstream.calls)


@pytest.mark.asyncio
async def test_cancelled_farewell_still_stops_typing_and_removes_session():
    upstream = Upstream()
    manager = Conversations(upstream)
    handle = (await manager.set_context(lambda: ALICE, 7, "topic")).data["handle"]
    original = upstream.execute
    entered = asyncio.Event()

    async def blocked(principal, operation, arguments):
        if operation == "send-message":
            entered.set()
            await asyncio.Event().wait()
        return await original(principal, operation, arguments)

    upstream.execute = blocked
    task = asyncio.create_task(manager.end(lambda: ALICE, handle))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not manager._sessions
    assert upstream.calls[-1][1] == "set-typing-status"
    assert upstream.calls[-1][2]["op"] == "stop"


class Queues:
    def __init__(self, upstream):
        self.upstream = upstream
        self.registrations = 0
        self.deleted = []
        self.results = []
        self.on_register = None
        self.blocked = None

    async def register(self, principal, arguments):
        self.registrations += 1
        self.upstream.calls.append((principal, "register-queue", arguments))
        if self.on_register:
            self.on_register(self.registrations)
        return OperationResult(
            "test", data={"handle": f"q{self.registrations}", "last_event_id": -1}
        )

    async def poll(self, principal, handle, cursor, *, wait_seconds):
        assert 0 < wait_seconds <= 25
        if self.blocked:
            self.blocked.set()
            await asyncio.Event().wait()
        return self.results.pop(0)

    async def delete(self, principal, handle):
        self.deleted.append(handle)
        return OperationResult("test", data={"deleted": True})


def event_result(mid, event_id=0):
    return OperationResult(
        "test",
        data={
            "events": [{"id": event_id, "type": "message", "message": message(mid)}],
            "next_event_id": event_id,
            "timed_out": False,
            "resync_required": False,
        },
    )


@pytest.mark.asyncio
async def test_listen_registers_before_catchup_and_cleans_queue():
    upstream = Upstream()
    queues = Queues(upstream)
    manager = Conversations(upstream, queues=queues)
    handle = (await manager.set_context(lambda: ALICE, 7, "topic")).data["handle"]
    upstream.messages = [message(11)]
    result = await manager.listen(lambda: ALICE, handle, timeout_seconds=2)
    assert [m["id"] for m in result.data["messages"]] == [11]
    ops = [op for _, op, _ in upstream.calls]
    assert ops.index("register-queue") < len(ops) - 1 - ops[::-1].index("get-messages")
    assert queues.deleted == ["q1"]


@pytest.mark.asyncio
async def test_expired_queue_registers_again_then_resynchronizes_history():
    upstream = Upstream()
    queues = Queues(upstream)
    manager = Conversations(upstream, queues=queues)
    handle = (await manager.set_context(lambda: ALICE, 7, "topic")).data["handle"]
    upstream.messages = []
    queues.results = [
        OperationResult(
            "test", error={"code": "queue_expired"}, data={"resync_required": True}
        )
    ]

    def renewed(count):
        if count == 2:
            upstream.messages = [message(11)]

    queues.on_register = renewed
    result = await manager.listen(lambda: ALICE, handle, timeout_seconds=2)
    assert result.data["queue_renewed"] is True
    assert result.data["messages"][0]["id"] == 11
    assert queues.deleted[-1] == "q2"


@pytest.mark.asyncio
async def test_listen_poll_delivers_messages_and_end_cancels_active_listener():
    upstream = Upstream()
    queues = Queues(upstream)
    manager = Conversations(upstream, queues=queues)
    handle = (await manager.set_context(lambda: ALICE, 7, "topic")).data["handle"]
    upstream.messages = []
    queues.results = [event_result(11)]
    result = await manager.listen(lambda: ALICE, handle, timeout_seconds=2)
    assert result.data["messages"][0]["id"] == 11
    queues.blocked = asyncio.Event()
    task = asyncio.create_task(manager.listen(lambda: ALICE, handle, timeout_seconds=2))
    await asyncio.wait_for(queues.blocked.wait(), 1)
    ended = await manager.end(lambda: ALICE, handle, "")
    assert ended.data["ended"]
    assert task.cancelled()
    assert queues.deleted == ["q1", "q2"]


@pytest.mark.asyncio
async def test_reactions_from_other_topics_cannot_dismiss_current_conversation():
    upstream = Upstream()
    queues = Queues(upstream)
    manager = Conversations(upstream, queues=queues)
    handle = (await manager.set_context(lambda: ALICE, 7, "topic")).data["handle"]
    upstream.messages = []
    upstream.last_message = message(20, sender=1, subject="other")
    queues.results = [
        OperationResult(
            "test",
            data={
                "events": [
                    {
                        "id": 0,
                        "type": "reaction",
                        "op": "add",
                        "message_id": 20,
                        "user_id": 2,
                        "emoji_name": "stop_sign",
                    }
                ],
                "next_event_id": 0,
            },
        ),
        event_result(11, 1),
    ]
    result = await manager.listen(lambda: ALICE, handle, timeout_seconds=2)
    assert not result.error
    assert result.data["messages"][0]["id"] == 11


@pytest.mark.asyncio
async def test_shutdown_cancels_active_reply_before_removing_session():
    upstream = Upstream()
    manager = Conversations(upstream)
    handle = (await manager.set_context(lambda: ALICE, 7, "topic")).data["handle"]
    upstream.messages = []
    original = upstream.execute
    entered = asyncio.Event()

    async def blocked(principal, operation, arguments):
        if operation == "send-message":
            entered.set()
            await asyncio.Event().wait()
        return await original(principal, operation, arguments)

    upstream.execute = blocked
    task = asyncio.create_task(manager.reply(lambda: ALICE, handle, "hello"))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        await manager.close()
        assert task.done(), "shutdown left an upstream write running"
        assert task.result().uncertain_write
        assert task.result().error["code"] == "conversation_cancelled"
        assert not manager._sessions
        assert upstream.calls[-1][2]["op"] == "stop"
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["event", "timeout", "renewal"])
async def test_listen_preserves_history_gap_across_empty_catchup(outcome):
    upstream = Upstream()
    queues = Queues(upstream)
    manager = Conversations(upstream, queues=queues)
    handle = (await manager.set_context(lambda: ALICE, 7, "topic")).data["handle"]
    upstream.messages = []
    original = upstream.execute
    history_reads = 0

    async def limited(principal, operation, arguments):
        nonlocal history_reads
        result = await original(principal, operation, arguments)
        if operation == "get-messages":
            history_reads += 1
            result.data["history_limited"] = history_reads == 1
        return result

    upstream.execute = limited
    if outcome == "timeout":
        queues.blocked = asyncio.Event()
    else:
        queues.results = [event_result(11)]
        if outcome == "renewal":
            queues.results.insert(
                0, OperationResult("test", error={"code": "queue_expired"})
            )
    result = await manager.listen(lambda: ALICE, handle, timeout_seconds=0.05)
    assert result.error is None
    assert result.data["history_limited"] is True
    assert result.data["resync_required"] is True
    assert result.data["timed_out"] == (outcome == "timeout")


@pytest.mark.parametrize(
    "options",
    [
        {"max_sessions": True},
        {"max_sessions": 1.5},
        {"message_max_length": False},
        {"message_max_length": float("nan")},
        {"auto_typing_start": "false"},
        {"prefix": 1},
        {"dismiss_emoji": {1}},
        {"dismiss_emoji": None},
        {"on_reply": True},
        {"on_set_context": "callback"},
        {"on_session_end": []},
    ],
)
def test_invalid_conversation_configuration_rejected_before_use(options):
    with pytest.raises(ValueError):
        Conversations(Upstream(), **options)


@pytest.mark.parametrize("setting", [":stop_sign:", {":stop_sign:", ":"}])
@pytest.mark.asyncio
async def test_dismiss_emoji_accepts_normalized_string_or_set(setting):
    upstream = Upstream()
    manager = Conversations(upstream, dismiss_emoji=setting)
    assert manager.dismiss_emoji == frozenset({"stop_sign"})
    handle = (await manager.set_context(lambda: ALICE, 7, "topic")).data["handle"]
    upstream.messages = []
    await manager.reply(lambda: ALICE, handle, "hello")
    upstream.last_message["reactions"] = [{"emoji_name": "stop_sign", "user_id": 2}]
    assert (await manager.reply(lambda: ALICE, handle, "no")).error[
        "code"
    ] == "session_dismissed"


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"op": []}, {"message_id": True}])
async def test_listen_malformed_reaction_returns_error_and_cleans_queue(changes):
    upstream = Upstream()
    queues = Queues(upstream)
    manager = Conversations(upstream, queues=queues)
    handle = (await manager.set_context(lambda: ALICE, 7, "topic")).data["handle"]
    upstream.messages = []
    queues.results = [
        OperationResult(
            "test",
            data={
                "events": [
                    {
                        "id": 0,
                        "type": "reaction",
                        "op": "add",
                        "message_id": 20,
                        "user_id": 2,
                        "emoji_name": "stop_sign",
                        **changes,
                    }
                ],
                "next_event_id": 0,
            },
        )
    ]
    result = await manager.listen(lambda: ALICE, handle, timeout_seconds=1)
    assert result.error["code"] == "invalid_reaction_event"
    assert queues.deleted == ["q1"]


@pytest.mark.asyncio
async def test_context_and_listen_include_formatted_messages():
    upstream = Upstream()
    queues = Queues(upstream)
    manager = Conversations(upstream, queues=queues, timezone="Europe/Prague")
    context = await manager.set_context(lambda: ALICE, 7, "topic")
    assert "<msg " in context.data["formatted_messages"]
    assert "CET" in context.data["formatted_messages"]
    upstream.messages = [message(11, "<untrusted>")]
    result = await manager.listen(
        lambda: ALICE, context.data["handle"], timeout_seconds=1
    )
    assert "&lt;untrusted&gt;" in result.data["formatted_messages"]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["history", "event", "reply"])
async def test_formatting_failure_preserves_cursor_and_returns_structured_error(source):
    upstream = Upstream()
    queues = Queues(upstream)
    manager = Conversations(upstream, queues=queues)
    handle = (await manager.set_context(lambda: ALICE, 7, "topic")).data["handle"]
    malformed = message(11, timestamp=-1)
    upstream.messages = [] if source == "event" else [malformed]
    if source == "event":
        event = event_result(11)
        event.data["events"][0]["message"] = malformed
        queues.results = [event]
    if source == "reply":
        result = await manager.reply(lambda: ALICE, handle, "do not send")
    else:
        result = await manager.listen(lambda: ALICE, handle, timeout_seconds=1)
    assert result.error["code"] == "invalid_conversation_response"
    assert manager._sessions[handle].last_seen == 10
    assert not any(op == "send-message" for _, op, _ in upstream.calls)


@pytest.mark.asyncio
async def test_invalid_context_history_does_not_start_typing_or_invoke_hook():
    upstream = Upstream()
    upstream.messages = [message(10, content="bad\x00")]
    hooks = []
    manager = Conversations(upstream, on_set_context=lambda *args: hooks.append(args))
    result = await manager.set_context(lambda: ALICE, 7, "topic")
    assert result.error["code"] == "invalid_conversation_response"
    assert not manager._sessions
    assert manager._pending == 0
    assert not hooks
    assert not any(op == "set-typing-status" for _, op, _ in upstream.calls)


@pytest.mark.asyncio
async def test_local_interrupt_consumed_once_without_polling(tmp_path):
    path = tmp_path / "interrupt"
    path.write_text("operator instruction")
    upstream = Upstream()
    queues = Queues(upstream)
    manager = Conversations(upstream, queues=queues, interrupt_file=path)
    handle = (await manager.set_context(lambda: ALICE, 7, "topic")).data["handle"]
    upstream.messages = []
    result = await manager.listen(lambda: ALICE, handle, timeout_seconds=1)
    assert result.data["interrupted"] is True
    assert result.data["interrupt_content"] == "operator instruction"
    assert not path.exists()
    queues.results = [event_result(11)]
    result = await manager.listen(lambda: ALICE, handle, timeout_seconds=1)
    assert result.data["messages"][0]["id"] == 11


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["symlink", "oversize"])
async def test_interrupt_rejects_unsafe_files_without_reading_or_deleting_target(
    tmp_path, kind
):
    path = tmp_path / "interrupt"
    target = tmp_path / "target"
    target.write_text("must remain")
    if kind == "symlink":
        path.symlink_to(target)
    else:
        path.write_bytes(b"x" * 65537)
    upstream = Upstream()
    manager = Conversations(upstream, queues=Queues(upstream), interrupt_file=path)
    handle = (await manager.set_context(lambda: ALICE, 7, "topic")).data["handle"]
    result = await manager.listen(lambda: ALICE, handle, timeout_seconds=1)
    assert result.error["code"] == "interrupt_file_error"
    assert target.read_text() == "must remain"
    assert list(tmp_path.glob("interrupt.*.claimed"))


def test_http_configuration_rejects_shared_interrupt_file(tmp_path):
    from thinkhome_zulip_mcp.server import create_server

    with pytest.raises(ValueError, match="interrupt_file.*stdio"):
        create_server(
            authenticator=object(),
            conversation_options={"interrupt_file": tmp_path / "interrupt"},
        )


@pytest.mark.asyncio
async def test_interrupt_created_during_poll_is_delivered_and_queue_cleaned(tmp_path):
    path = tmp_path / "interrupt"
    upstream = Upstream()
    queues = Queues(upstream)
    manager = Conversations(upstream, queues=queues, interrupt_file=path)
    handle = (await manager.set_context(lambda: ALICE, 7, "topic")).data["handle"]
    upstream.messages = []

    async def poll(principal, handle, cursor, *, wait_seconds):
        assert wait_seconds <= 1
        path.write_bytes(b"operator \xff")
        return OperationResult(
            "test", data={"events": [], "next_event_id": cursor, "timed_out": True}
        )

    queues.poll = poll
    result = await manager.listen(lambda: ALICE, handle, timeout_seconds=2)
    assert result.data["interrupt_content"] == "operator \ufffd"
    assert queues.deleted == ["q1"]
    assert not path.exists()


def test_consuming_interrupt_does_not_delete_replacement_at_original_path(
    tmp_path, monkeypatch
):
    from pathlib import Path

    path = tmp_path / "interrupt"
    path.write_text("first")
    rename = Path.rename

    def replace_during_claim(source, destination):
        result = rename(source, destination)
        path.write_text("second")
        return result

    monkeypatch.setattr(Path, "rename", replace_during_claim)
    manager = Conversations(Upstream(), interrupt_file=path)
    assert manager._consume_interrupt() == "first"
    assert path.read_text() == "second"


@pytest.mark.asyncio
async def test_exit_markers_record_last_reply_without_following_symlinks(tmp_path):
    target = tmp_path / "unrelated"
    target.write_text("preserve")
    (tmp_path / ".clean_exit").symlink_to(target)
    upstream = Upstream()
    manager = Conversations(upstream, workspace_path=tmp_path)
    handle = (await manager.set_context(lambda: ALICE, 7, "topic")).data["handle"]
    upstream.messages = []
    await manager.reply(lambda: ALICE, handle, "reply")
    result = await manager.end(lambda: ALICE, handle, "")
    assert result.data["ended"]
    assert float((tmp_path / ".clean_exit").read_text()) > 0
    assert (tmp_path / ".last_sent_message_id").read_text() == "20"
    assert target.read_text() == "preserve"
    handle = (
        await manager.set_context(lambda: ALICE, 7, "topic", num_messages=0)
    ).data["handle"]
    await manager.end(lambda: ALICE, handle, "")
    assert not (tmp_path / ".last_sent_message_id").exists()


@pytest.mark.asyncio
async def test_exit_marker_failure_does_not_prevent_session_cleanup(tmp_path):
    upstream = Upstream()
    manager = Conversations(upstream, workspace_path=tmp_path / "missing")
    handle = (await manager.set_context(lambda: ALICE, 7, "topic")).data["handle"]
    result = await manager.end(lambda: ALICE, handle, "")
    assert result.data["ended"]
    assert "exit_marker_failed" in result.data["warnings"]
    assert not manager._sessions


def test_http_rejects_shared_exit_marker_workspace(tmp_path):
    from thinkhome_zulip_mcp.server import create_server

    with pytest.raises(ValueError, match="workspace_path.*stdio"):
        create_server(
            authenticator=object(), conversation_options={"workspace_path": tmp_path}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("topic", ["topic", "other"])
async def test_trigger_message_is_verified_and_sets_requester_and_cursor(topic):
    upstream = Upstream()
    upstream.last_message = message(17, sender=4, subject=topic)
    manager = Conversations(upstream)
    result = await manager.set_context(lambda: ALICE, 7, "topic", trigger_message_id=17)
    if topic == "topic":
        assert not result.error
        assert result.data["requester_user_id"] == 4
        assert result.data["context"]["last_seen_message_id"] == 17
    else:
        assert result.error
        assert not manager._sessions
        assert not any(op == "set-typing-status" for _, op, _ in upstream.calls)


@pytest.mark.asyncio
async def test_reaction_target_boolean_id_is_rejected_without_dismissal():
    upstream = Upstream()
    queues = Queues(upstream)
    manager = Conversations(upstream, queues=queues)
    handle = (await manager.set_context(lambda: ALICE, 7, "topic")).data["handle"]
    upstream.messages = []
    upstream.last_message = message(True, sender=1)
    queues.results = [
        OperationResult(
            "test",
            data={
                "events": [
                    {
                        "id": 0,
                        "type": "reaction",
                        "op": "add",
                        "message_id": 1,
                        "user_id": 2,
                        "emoji_name": "stop_sign",
                    }
                ],
                "next_event_id": 0,
            },
        )
    ]
    result = await manager.listen(lambda: ALICE, handle, timeout_seconds=1)
    assert result.error["code"] == "invalid_reaction_target"
    assert not manager._sessions[handle].dismissed


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", [60, 7200])
async def test_listen_renews_bounded_polls_for_entire_requested_duration(
    monkeypatch, duration
):
    import thinkhome_zulip_mcp.conversations as conversations_module

    now = 100.0
    monkeypatch.setattr(conversations_module, "monotonic", lambda: now)
    upstream = Upstream()
    queues = Queues(upstream)
    manager = Conversations(upstream, queues=queues)
    handle = (await manager.set_context(lambda: ALICE, 7, "topic")).data["handle"]
    upstream.messages = []
    waits = []
    progress = []

    async def poll(principal, handle, cursor, *, wait_seconds):
        nonlocal now
        assert 0 < wait_seconds <= 25
        waits.append(wait_seconds)
        now += wait_seconds
        return OperationResult(
            "test",
            data={
                "events": [],
                "next_event_id": cursor,
                "timed_out": True,
            },
        )

    queues.poll = poll
    result = await manager.listen(
        lambda: ALICE,
        handle,
        timeout_seconds=duration,
        progress=lambda elapsed, total: progress.append((elapsed, total)),
    )
    assert result.data["timed_out"]
    assert sum(waits) == duration
    assert len(waits) == (duration + 24) // 25
    assert progress[-1] == (duration, duration)
    assert queues.deleted == ["q1"]


@pytest.mark.asyncio
async def test_reply_rechecks_identity_before_publishing_partial_catchup():
    upstream = Upstream()
    manager = Conversations(upstream)
    actor = ALICE
    handle = (await manager.set_context(lambda: actor, 7, "topic")).data["handle"]
    original = upstream.execute
    upstream.messages, upstream.more = [message(11, "sensitive history")], True

    async def revoke(principal, operation, arguments):
        nonlocal actor
        result = await original(principal, operation, arguments)
        if operation == "get-messages":
            actor = BOB
        return result

    upstream.execute = revoke
    result = await manager.reply(lambda: actor, handle, "hello")
    assert result.error["code"] == "authentication_required"
    assert result.data is None
    assert manager._sessions[handle].last_seen == 10
    assert not any(op == "send-message" for _, op, _ in upstream.calls)

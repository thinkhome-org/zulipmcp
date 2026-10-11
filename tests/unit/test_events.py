import asyncio

import pytest

from thinkhome_zulip_mcp.executor import OperationResult, Principal

ALICE = Principal("alice", "https://zulip.example", "alice", "synthetic")
BOB = Principal("bob", "https://zulip.example", "bob", "synthetic")


class Upstream:
    def __init__(self):
        self.calls = []
        self.expired = False
        self.block = False
        self.entered = asyncio.Event()
        self.cancelled = False

    async def execute(self, principal, operation, arguments):
        self.calls.append((principal.identity_id, operation, arguments))
        if operation == "register-queue":
            return OperationResult(
                "test", data={"queue_id": "upstream", "last_event_id": -1}
            )
        if operation == "get-events":
            self.entered.set()
            if self.block:
                try:
                    await asyncio.Event().wait()
                finally:
                    self.cancelled = True
            if self.expired:
                return OperationResult(
                    "test",
                    data={"code": "BAD_EVENT_QUEUE_ID"},
                    error={"code": "upstream_error"},
                )
            return OperationResult(
                "test",
                data={
                    "events": [
                        {"id": 2, "type": "message"},
                        {"id": 5, "type": "heartbeat"},
                    ]
                },
            )
        return OperationResult("test", data={"result": "success"})


@pytest.mark.asyncio
async def test_queue_is_principal_scoped_and_cursor_is_explicit():
    from thinkhome_zulip_mcp.events import EventQueues

    upstream = Upstream()
    queues = EventQueues(upstream)
    registered = await queues.register(ALICE, {"event_types": ["message"]})
    handle = registered.data["handle"]
    assert "queue_id" not in registered.data
    denied = await queues.poll(BOB, handle, -1, wait_seconds=0)
    assert denied.error["code"] == "invalid_queue_handle"
    assert len(upstream.calls) == 1
    for _ in range(2):
        result = await queues.poll(ALICE, handle, -1, wait_seconds=0)
        assert result.data["next_event_id"] == 5
        assert result.data["resync_required"] is False
        assert upstream.calls[-1][2]["last_event_id"] == -1
    assert (await queues.delete(BOB, handle)).error
    assert (await queues.delete(ALICE, handle)).error is None
    assert (await queues.poll(ALICE, handle, -1)).error


@pytest.mark.asyncio
async def test_queue_expiry_is_explicit():
    from thinkhome_zulip_mcp.events import EventQueues

    upstream = Upstream()
    queues = EventQueues(upstream)
    handle = (await queues.register(ALICE, {})).data["handle"]
    upstream.expired = True
    result = await queues.poll(ALICE, handle, -1)
    assert result.error["code"] == "queue_expired"
    assert result.data["resync_required"] is True
    assert len([c for c in upstream.calls if c[1] == "register-queue"]) == 1


@pytest.mark.asyncio
async def test_cancel_releases_wait_and_delete_cancels_pending_poll():
    from thinkhome_zulip_mcp.events import EventQueues

    upstream = Upstream()
    queues = EventQueues(upstream)
    handle = (await queues.register(ALICE, {})).data["handle"]
    upstream.block = True
    task = asyncio.create_task(queues.poll(ALICE, handle, -1))
    await upstream.entered.wait()
    assert (await queues.poll(ALICE, handle, -1)).error["code"] == "queue_busy"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert upstream.cancelled
    upstream.entered.clear()
    task = asyncio.create_task(queues.poll(ALICE, handle, -1))
    await upstream.entered.wait()
    assert (await queues.delete(ALICE, handle)).error is None
    assert task.cancelled()


@pytest.mark.asyncio
async def test_poll_timeout_does_not_advance_cursor_or_report_expiry():
    from thinkhome_zulip_mcp.events import EventQueues

    upstream = Upstream()
    queues = EventQueues(upstream)
    handle = (await queues.register(ALICE, {})).data["handle"]
    upstream.block = True
    result = await queues.poll(ALICE, handle, 7, wait_seconds=0.01)
    assert result.error is None
    assert result.data == {
        "events": [],
        "next_event_id": 7,
        "timed_out": True,
        "resync_required": False,
    }
    assert upstream.cancelled
    for wait in (-1, 26, float("nan"), float("inf")):
        assert (await queues.poll(ALICE, handle, 7, wait_seconds=wait)).error
    await queues.close()
    assert upstream.calls[-1][1] == "delete-queue"


@pytest.mark.asyncio
async def test_queue_limits_and_shutdown():
    from thinkhome_zulip_mcp.events import EventQueues

    upstream = Upstream()
    queues = EventQueues(upstream, max_queues=1)
    await queues.register(ALICE, {})
    assert (await queues.register(BOB, {})).error["code"] == "queue_limit_exceeded"
    await queues.close()
    assert upstream.calls[-1][:2] == ("alice", "delete-queue")
    assert (await queues.register(ALICE, {})).error["code"] == "queue_manager_closed"


@pytest.mark.asyncio
async def test_registration_reserves_capacity_during_network_wait():
    from thinkhome_zulip_mcp.events import EventQueues

    upstream = Upstream()
    original = upstream.execute
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(principal, operation, arguments):
        if operation == "register-queue":
            entered.set()
            await release.wait()
        return await original(principal, operation, arguments)

    upstream.execute = delayed
    queues = EventQueues(upstream, max_queues=1)
    pending = asyncio.create_task(queues.register(ALICE, {}))
    await entered.wait()
    assert (await queues.register(BOB, {})).error["code"] == "queue_limit_exceeded"
    await queues.close()
    release.set()
    assert (await pending).error is not None
    assert upstream.calls[-1][1] == "delete-queue"


@pytest.mark.asyncio
async def test_invalid_response_never_advances_cursor_and_failed_delete_can_retry():
    from thinkhome_zulip_mcp.events import EventQueues

    upstream = Upstream()
    original = upstream.execute
    queues = EventQueues(upstream)
    handle = (await queues.register(ALICE, {})).data["handle"]
    failures = True

    async def broken(principal, operation, arguments):
        if operation == "get-events":
            return OperationResult("test", data={"events": [{"id": 5}, {"id": 2}]})
        if operation == "delete-queue" and failures:
            return OperationResult("test", error={"code": "upstream_transport_error"})
        return await original(principal, operation, arguments)

    upstream.execute = broken
    assert (await queues.poll(ALICE, handle, -1)).error[
        "code"
    ] == "invalid_events_response"
    assert (await queues.delete(ALICE, handle)).error
    failures = False
    assert (await queues.delete(ALICE, handle)).error is None


@pytest.mark.asyncio
async def test_same_identity_in_another_realm_cannot_use_queue():
    from thinkhome_zulip_mcp.events import EventQueues

    queues = EventQueues(Upstream())
    handle = (await queues.register(ALICE, {})).data["handle"]
    other = Principal("alice", "https://other.example", "alice", "synthetic")
    assert (await queues.poll(other, handle, -1)).error[
        "code"
    ] == "invalid_queue_handle"
    await queues.close()


@pytest.mark.asyncio
async def test_shutdown_cancels_all_polls_even_when_upstream_delete_stalls(monkeypatch):
    from thinkhome_zulip_mcp.events import EventQueues

    upstream = Upstream()
    queues = EventQueues(upstream)
    alice_handle = (await queues.register(ALICE, {})).data["handle"]
    bob_handle = (await queues.register(BOB, {})).data["handle"]
    original = upstream.execute

    async def blocked_delete(principal, operation, arguments):
        if operation == "delete-queue":
            await asyncio.Event().wait()
        return await original(principal, operation, arguments)

    upstream.execute = blocked_delete
    upstream.block = True
    tasks = [
        asyncio.create_task(queues.poll(ALICE, alice_handle, -1)),
        asyncio.create_task(queues.poll(BOB, bob_handle, -1)),
    ]
    await asyncio.sleep(0)
    timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda seconds: timeout(0.01))
    try:
        await queues.close()
        assert all(task.cancelled() for task in tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_invalid_queue_arguments_and_registration_metadata_are_rejected():
    from thinkhome_zulip_mcp.events import EventQueues

    upstream = Upstream()
    queues = EventQueues(upstream)
    handle = (await queues.register(ALICE, {})).data["handle"]
    for wait in ("25", None, True):
        assert (await queues.poll(ALICE, handle, -1, wait_seconds=wait)).error
    for cursor in (-2, True, "-1"):
        assert (await queues.poll(ALICE, handle, cursor)).error
    original = upstream.execute

    async def malformed(principal, operation, arguments):
        if operation == "register-queue":
            return OperationResult(
                "test", data={"queue_id": "bad", "last_event_id": -5}
            )
        return await original(principal, operation, arguments)

    upstream.execute = malformed
    assert (await queues.register(ALICE, {})).error["code"] == "invalid_queue_response"
    assert upstream.calls[-1][1] == "delete-queue"
    await queues.close()


@pytest.mark.asyncio
async def test_malformed_registration_reports_uncertain_creation():
    from thinkhome_zulip_mcp.events import EventQueues

    upstream = Upstream()

    async def malformed(principal, operation, arguments):
        return OperationResult("test", data={"result": "success"})

    upstream.execute = malformed
    result = await EventQueues(upstream).register(ALICE, {})
    assert result.error["code"] == "invalid_queue_response"
    assert result.uncertain_write

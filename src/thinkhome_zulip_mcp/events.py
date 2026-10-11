"""Caller-owned event queues with explicit acknowledgements and bounded waits."""

import asyncio
import math
import secrets
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from thinkhome_zulip_mcp.executor import Executor, OperationResult, Principal


def _error(
    code: str, *, resync: bool = False, uncertain: bool = False
) -> OperationResult:
    return OperationResult(
        str(uuid4()),
        error={"code": code},
        data={"resync_required": resync},
        uncertain_write=uncertain,
    )


def _expired(result: OperationResult) -> bool:
    return (
        isinstance(result.data, dict)
        and result.data.get("code") == "BAD_EVENT_QUEUE_ID"
    )


@dataclass
class _Queue:
    principal: Principal
    queue_id: str
    waiter: asyncio.Task[Any] | None = None
    closing: bool = False


class EventQueues:
    def __init__(self, executor: Executor, *, max_queues: int = 100) -> None:
        if max_queues <= 0:
            raise ValueError("Queue limit must be positive")
        self.executor = executor
        self.max_queues = max_queues
        self._queues: dict[str, _Queue] = {}
        self._pending = 0
        self._closed = False

    def _owned(self, principal: Principal, handle: str) -> _Queue | None:
        queue = self._queues.get(handle)
        if queue is None or (
            queue.principal.identity_id != principal.identity_id
            or queue.principal.realm_url.rstrip("/") != principal.realm_url.rstrip("/")
        ):
            return None
        return queue

    async def register(
        self, principal: Principal, arguments: dict[str, Any]
    ) -> OperationResult:
        if self._closed:
            return _error("queue_manager_closed")
        if len(self._queues) + self._pending >= self.max_queues:
            return _error("queue_limit_exceeded")
        self._pending += 1
        try:
            result = await self.executor.execute(principal, "register-queue", arguments)
            if result.error:
                return result
            data = result.data
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("queue_id"), str)
                or not data["queue_id"]
            ):
                return _error("invalid_queue_response", resync=True, uncertain=True)
            queue_id = data["queue_id"]
            if (
                self._closed
                or type(data.get("last_event_id")) is not int
                or data["last_event_id"] < -1
            ):
                cleanup = await self.executor.execute(
                    principal, "delete-queue", {"queue_id": queue_id}
                )
                return _error(
                    "invalid_queue_response",
                    resync=True,
                    uncertain=cleanup.error is not None and not _expired(cleanup),
                )
            handle = secrets.token_urlsafe(32)
            self._queues[handle] = _Queue(principal, queue_id)
            return OperationResult(
                result.request_id,
                data={
                    **{k: v for k, v in data.items() if k != "queue_id"},
                    "handle": handle,
                    "resync_required": False,
                },
                status_code=result.status_code,
            )
        finally:
            self._pending -= 1

    async def poll(
        self,
        principal: Principal,
        handle: str,
        last_event_id: int,
        *,
        wait_seconds: float = 25,
    ) -> OperationResult:
        if (
            type(last_event_id) is not int
            or last_event_id < -1
            or type(wait_seconds) not in {int, float}
            or not math.isfinite(wait_seconds)
            or not 0 <= wait_seconds <= 25
        ):
            return _error("invalid_event_arguments")
        queue = self._owned(principal, handle)
        if queue is None or self._closed:
            return _error("invalid_queue_handle", resync=True)
        if queue.waiter or queue.closing:
            return _error("queue_busy")
        queue.principal = principal
        queue.waiter = asyncio.current_task()
        try:
            try:
                async with asyncio.timeout(wait_seconds if wait_seconds else 25):
                    result = await self.executor.execute(
                        principal,
                        "get-events",
                        {
                            "queue_id": queue.queue_id,
                            "last_event_id": last_event_id,
                            "dont_block": wait_seconds == 0,
                        },
                    )
            except TimeoutError:
                return OperationResult(
                    str(uuid4()),
                    data={
                        "events": [],
                        "next_event_id": last_event_id,
                        "timed_out": True,
                        "resync_required": False,
                    },
                )
            if _expired(result):
                self._queues.pop(handle, None)
                return _error("queue_expired", resync=True)
            if result.error:
                return result
            events = (
                result.data.get("events") if isinstance(result.data, dict) else None
            )
            if not isinstance(events, list):
                return _error("invalid_events_response", resync=True)
            cursor = last_event_id
            for event in events:
                if (
                    not isinstance(event, dict)
                    or type(event.get("id")) is not int
                    or event["id"] <= cursor
                ):
                    return _error("invalid_events_response", resync=True)
                cursor = event["id"]
            return OperationResult(
                result.request_id,
                data={
                    "events": events,
                    "next_event_id": cursor,
                    "timed_out": False,
                    "resync_required": False,
                },
                status_code=result.status_code,
            )
        finally:
            queue.waiter = None

    async def delete(self, principal: Principal, handle: str) -> OperationResult:
        queue = self._owned(principal, handle)
        if queue is None:
            return _error("invalid_queue_handle")
        if queue.closing:
            return _error("queue_busy")
        queue.closing = True
        try:
            if queue.waiter:
                queue.waiter.cancel()
                with suppress(asyncio.CancelledError):
                    await queue.waiter
            result = await self.executor.execute(
                principal, "delete-queue", {"queue_id": queue.queue_id}
            )
            if not result.error or _expired(result):
                self._queues.pop(handle, None)
                return OperationResult(
                    result.request_id,
                    data={"deleted": True},
                    status_code=result.status_code,
                )
            return result
        finally:
            queue.closing = False

    async def close(self) -> None:
        self._closed = True
        try:
            async with asyncio.timeout(5):
                waiters = [q.waiter for q in self._queues.values() if q.waiter]
                for waiter in waiters:
                    waiter.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)
                for handle, queue in list(self._queues.items()):
                    await self.delete(queue.principal, handle)
        except TimeoutError:
            pass  # Remaining server-side queues expire under Zulip's idle timeout.
        finally:
            self._queues.clear()

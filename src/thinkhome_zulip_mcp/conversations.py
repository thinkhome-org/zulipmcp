"""Caller-owned conversations; cursors advance only for delivered history."""

import asyncio
import inspect
import math
import os
import secrets
import stat
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import monotonic, time
from typing import Any, TypeGuard
from uuid import uuid4
from zoneinfo import ZoneInfo

from thinkhome_zulip_mcp.events import EventQueues
from thinkhome_zulip_mcp.executor import Executor, OperationResult, Principal
from thinkhome_zulip_mcp.formatting import format_messages
from thinkhome_zulip_mcp.markdown import normalize_zulip_markdown, prepare_message
from thinkhome_zulip_mcp.store import AuthenticationError
from thinkhome_zulip_mcp.workflows import history_page

PrincipalProvider = Callable[[], Principal]


def _error(code: str, data: Any = None) -> OperationResult:
    return OperationResult(str(uuid4()), data=data, error={"code": code})


def _owner(principal: Principal) -> tuple[str, str]:
    return principal.identity_id, principal.realm_url.rstrip("/")


def _positive(value: Any) -> TypeGuard[int]:
    return type(value) is int and value > 0


@dataclass
class _Conversation:
    principal: Principal = field(repr=False)
    user_id: int
    stream_id: int
    stream: str
    private: bool
    topic: str
    last_seen: int = 0
    last_sent: int | None = None
    dismissed: bool = False
    started: float = field(default_factory=monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    waiter: asyncio.Task[Any] | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "stream_id": self.stream_id,
            "stream": self.stream,
            "topic": self.topic,
            "user_id": self.user_id,
            "private": self.private,
            "last_seen_message_id": self.last_seen,
            "last_sent_message_id": self.last_sent,
            "dismissed": self.dismissed,
            "elapsed_seconds": int(monotonic() - self.started),
        }


class Conversations:
    def __init__(
        self,
        executor: Executor,
        *,
        queues: EventQueues | None = None,
        max_sessions: int = 100,
        timezone: str = "UTC",
        interrupt_file: str | Path | None = None,
        workspace_path: str | Path | None = None,
        message_max_length: int = 10000,
        prefix: str | Callable[[], Any] = "",
        auto_typing_start: bool = True,
        dismiss_emoji: str | set[str] | frozenset[str] = frozenset({"stop_sign"}),
        on_set_context: Callable[..., Any] | None = None,
        on_reply: Callable[..., Any] | None = None,
        on_session_end: Callable[..., Any] | None = None,
    ) -> None:
        if not _positive(max_sessions) or not _positive(message_max_length):
            raise ValueError("Conversation limits must be positive integers")
        if type(auto_typing_start) is not bool:
            raise ValueError("auto_typing_start must be a boolean")
        if not isinstance(prefix, str) and not callable(prefix):
            raise ValueError("prefix must be a string or callable")
        for hook in (on_set_context, on_reply, on_session_end):
            if hook is not None and not callable(hook):
                raise ValueError("Conversation hooks must be callable")
        if isinstance(dismiss_emoji, str):
            dismiss_emoji = {dismiss_emoji}
        if not isinstance(dismiss_emoji, (set, frozenset)) or any(
            not isinstance(name, str) for name in dismiss_emoji
        ):
            raise ValueError("dismiss_emoji must be a string or set of strings")
        ZoneInfo(timezone)
        self.timezone = timezone
        self.workspace_path = (
            Path(workspace_path) if workspace_path is not None else None
        )
        self.interrupt_file = (
            Path(interrupt_file) if interrupt_file is not None else None
        )
        self.executor = executor
        self.queues = queues or EventQueues(executor)
        self._owns_queues = queues is None
        self.max_sessions = max_sessions
        self.message_max_length = message_max_length
        self.prefix = prefix
        self.auto_typing_start = auto_typing_start
        self.dismiss_emoji = frozenset(
            name.strip(":") for name in dismiss_emoji if name.strip(":")
        )
        self.on_set_context = on_set_context
        self.on_reply = on_reply
        self.on_session_end = on_session_end
        self._sessions: dict[str, _Conversation] = {}
        self._pending = 0
        self._closed = False

    def _write_exit_markers(self, session: _Conversation, warnings: list[str]) -> None:
        workspace = self.workspace_path
        if workspace is None:
            return
        temporary: Path | None = None
        try:
            last_sent = workspace / ".last_sent_message_id"
            if session.last_sent is None:
                last_sent.unlink(missing_ok=True)
            markers = {".clean_exit": str(time())}
            if session.last_sent is not None:
                markers = {".last_sent_message_id": str(session.last_sent), **markers}
            for name, value in markers.items():
                with NamedTemporaryFile(
                    mode="w", dir=workspace, delete=False
                ) as output:
                    temporary = Path(output.name)
                    output.write(value)
                temporary.replace(workspace / name)
                temporary = None
        except OSError:
            warnings.append("exit_marker_failed")
        finally:
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink()

    def _consume_interrupt(self) -> str | None:
        path = self.interrupt_file
        if path is None:
            return None
        claimed = path.with_name(f"{path.name}.{uuid4().hex}.claimed")
        try:
            path.rename(claimed)
        except FileNotFoundError:
            return None
        # Leave failed claims for operator inspection; never follow symbolic links.
        fd = os.open(claimed, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 65536:
                raise ValueError("Interrupt must be a regular file of at most 64 KiB")
            content = source.read(65537)
            if len(content) > 65536:
                raise ValueError("Interrupt exceeds 64 KiB")
        claimed.unlink()
        return content.decode("utf-8", errors="replace")

    async def _call(
        self,
        current: PrincipalProvider,
        owner: Principal,
        operation: str,
        arguments: dict[str, Any],
    ) -> OperationResult:
        principal = self._principal(current, owner)
        return await self.executor.execute(principal, operation, arguments)

    @staticmethod
    def _principal(current: PrincipalProvider, owner: Principal) -> Principal:
        principal = current()
        if _owner(principal) != _owner(owner):
            raise AuthenticationError("Conversation owner changed")
        return principal

    def _owned(self, current: PrincipalProvider, handle: str) -> _Conversation | None:
        principal = current()
        session = self._sessions.get(handle)
        if session is None or _owner(session.principal) != _owner(principal):
            return None
        session.principal = principal
        return session

    async def _hook(self, callback: Callable[..., Any] | None, *args: Any) -> Any:
        if callback is None:
            return None
        result = callback(*args)
        return await result if inspect.isawaitable(result) else result

    async def _content(self, content: str) -> str:
        prefix = await self._hook(self.prefix) if callable(self.prefix) else self.prefix
        if not isinstance(prefix, str) or not isinstance(content, str):
            raise ValueError("Invalid message prefix or content")
        return prepare_message(prefix + content, max_length=self.message_max_length)

    async def _typing(
        self,
        current: PrincipalProvider,
        session: _Conversation,
        op: str,
        warnings: list[str],
    ) -> None:
        try:
            result = await self._call(
                current,
                session.principal,
                "set-typing-status",
                {
                    "type": "stream",
                    "stream_id": session.stream_id,
                    "topic": session.topic,
                    "op": op,
                },
            )
            if result.error:
                warnings.append("typing_failed")
        except Exception:
            warnings.append("typing_failed")

    def _messages(
        self,
        session: _Conversation,
        messages: Any,
        *,
        exclude_self: bool = False,
    ) -> list[dict[str, Any]]:
        if not isinstance(messages, list):
            raise ValueError("Invalid conversation history")
        visible = []
        for message in messages:
            if (
                not isinstance(message, dict)
                or not _positive(message.get("id"))
                or not _positive(message.get("sender_id"))
                or message.get("type") != "stream"
                or type(message.get("stream_id")) is not int
                or message["stream_id"] != session.stream_id
                or not isinstance(message.get("subject"), str)
                or message["subject"].casefold() != session.topic.casefold()
                or not isinstance(message.get("content"), str)
            ):
                raise ValueError("Invalid conversation history")
            hidden = any(
                word in message["subject"].casefold()
                or message["content"].strip().casefold().startswith(word)
                for word in ("/nobots", "/nb")
            )
            if not hidden and not (
                exclude_self and message["sender_id"] == session.user_id
            ):
                visible.append(message)
        return visible

    async def set_context(
        self,
        current: PrincipalProvider,
        stream: str | int,
        topic: str,
        *,
        num_messages: int = 20,
        trigger_message_id: int | None = None,
    ) -> OperationResult:
        if self._closed:
            return _error("conversation_manager_closed")
        if (
            type(stream) not in {str, int}
            or (isinstance(stream, str) and not stream.strip())
            or (type(stream) is int and stream <= 0)
            or not isinstance(topic, str)
            or type(num_messages) is not int
            or not 0 <= num_messages <= 200
            or (trigger_message_id is not None and not _positive(trigger_message_id))
        ):
            return _error("invalid_context")
        if len(self._sessions) + self._pending >= self.max_sessions:
            return _error("conversation_limit_exceeded")
        self._pending += 1
        try:
            principal = current()
            own = await self._call(current, principal, "get-own-user", {})
            if own.error:
                return own
            if not isinstance(own.data, dict) or not _positive(own.data.get("user_id")):
                return _error("invalid_user_response")
            stream_id: str | int | None = stream
            if isinstance(stream, str):
                lookup = await self._call(
                    current, principal, "get-stream-id", {"stream": stream}
                )
                if lookup.error:
                    return lookup
                stream_id = (
                    lookup.data.get("stream_id")
                    if isinstance(lookup.data, dict)
                    else None
                )
            if not _positive(stream_id):
                return _error("invalid_channel_response")
            result = await self._call(
                current, principal, "get-stream-by-id", {"stream_id": stream_id}
            )
            if result.error:
                return result
            channel = (
                result.data.get("stream") if isinstance(result.data, dict) else None
            )
            if (
                not isinstance(channel, dict)
                or type(channel.get("stream_id")) is not int
                or channel["stream_id"] != stream_id
                or not isinstance(channel.get("name"), str)
                or type(channel.get("invite_only")) is not bool
            ):
                return _error("invalid_channel_response")
            session = _Conversation(
                principal,
                own.data["user_id"],
                channel["stream_id"],
                channel["name"],
                channel["invite_only"],
                topic,
            )
            initial_cursor = own.data.get("max_message_id")
            session.last_seen = initial_cursor if _positive(initial_cursor) else 0
            messages: list[dict[str, Any]] = []
            limited = False
            if num_messages:
                history = await history_page(
                    self.executor,
                    self._principal(current, principal),
                    self._narrow(session),
                    limit=num_messages,
                )
                if history.error:
                    return history
                messages = self._messages(session, history.data["messages"])
                page = history.data.get("policy_page", {})
                ids = [m["id"] for m in history.data["messages"]]
                if ids or page.get("last_id"):
                    session.last_seen = page.get("last_id") or ids[-1]
                limited = bool(history.data.get("history_limited", False))
            requester = None
            if trigger_message_id is not None:
                trigger = await self._call(
                    current,
                    principal,
                    "get-message",
                    {"message_id": trigger_message_id, "apply_markdown": False},
                )
                if trigger.error:
                    return trigger
                msg = (
                    trigger.data.get("message")
                    if isinstance(trigger.data, dict)
                    else None
                )
                if not isinstance(msg, dict):
                    return _error("invalid_trigger_message")
                self._messages(session, [msg])
                if msg["id"] != trigger_message_id:
                    return _error("invalid_trigger_message")
                session.last_seen = trigger_message_id
                requester = msg["sender_id"]
            if self._closed:
                return _error("conversation_manager_closed")
            # Recheck the caller after all setup reads, before publishing a handle.
            if _owner(current()) != _owner(principal):
                raise AuthenticationError("Conversation owner changed")
            formatted = format_messages(messages, timezone=self.timezone)
            warnings: list[str] = []
            extra = None
            try:
                try:
                    extra = await self._hook(
                        self.on_set_context, session.stream, session.topic
                    )
                except Exception:
                    warnings.append("on_set_context_failed")
                if self.auto_typing_start:
                    await self._typing(current, session, "start", warnings)
                self._principal(current, principal)
            except BaseException:
                async with asyncio.timeout(5):
                    await self._typing(current, session, "stop", warnings)
                raise
            if self._closed:
                await self._typing(current, session, "stop", warnings)
                return _error("conversation_manager_closed")
            handle = secrets.token_urlsafe(32)
            self._sessions[handle] = session
            return OperationResult(
                str(uuid4()),
                data={
                    "handle": handle,
                    "context": session.snapshot(),
                    "messages": messages,
                    "formatted_messages": formatted,
                    "history_limited": limited,
                    "requester_user_id": requester,
                    "context_extra": extra if isinstance(extra, str) else None,
                    "warnings": warnings,
                },
            )
        except AuthenticationError:
            return _error("authentication_required")
        except ValueError:
            return _error("invalid_conversation_response")
        finally:
            self._pending -= 1

    @staticmethod
    def _narrow(session: _Conversation) -> list[dict[str, Any]]:
        return [
            {"operator": "channel", "operand": session.stream_id},
            {"operator": "topic", "operand": session.topic},
        ]

    async def _dismissed(
        self, current: PrincipalProvider, session: _Conversation
    ) -> OperationResult | None:
        if session.dismissed:
            return _error("session_dismissed")
        if session.last_sent is None or not self.dismiss_emoji:
            return None
        result = await self._call(
            current,
            session.principal,
            "get-message",
            {"message_id": session.last_sent, "apply_markdown": False},
        )
        if result.error:
            return result
        message = result.data.get("message") if isinstance(result.data, dict) else None
        try:
            if not isinstance(message, dict):
                return _error("invalid_conversation_response")
            self._messages(session, [message])
            if (
                message["id"] != session.last_sent
                or message["sender_id"] != session.user_id
            ):
                raise ValueError("Invalid dismissal target")
            reactions = message.get("reactions", [])
            if not isinstance(reactions, list) or any(
                not isinstance(r, dict)
                or not _positive(r.get("user_id"))
                or not isinstance(r.get("emoji_name"), str)
                for r in reactions
            ):
                raise ValueError("Invalid reactions")
            for reaction in reactions:
                if (
                    reaction["user_id"] != session.user_id
                    and reaction["emoji_name"] in self.dismiss_emoji
                ):
                    session.dismissed = True
                    return _error(
                        "session_dismissed", {"emoji_name": reaction["emoji_name"]}
                    )
        except ValueError:
            return _error("invalid_conversation_response")
        return None

    async def _catchup(
        self, current: PrincipalProvider, session: _Conversation
    ) -> OperationResult:
        principal = current()
        if _owner(principal) != _owner(session.principal):
            raise AuthenticationError("Conversation owner changed")
        result = await history_page(
            self.executor,
            principal,
            self._narrow(session),
            cursor=session.last_seen or None,
            direction="newer",
            limit=200,
        )
        if result.error:
            return result
        try:
            visible = self._messages(
                session, result.data["messages"], exclude_self=True
            )
        except ValueError:
            return _error("invalid_conversation_response")
        result.data["messages"] = visible
        return result

    async def reply(
        self, current: PrincipalProvider, handle: str, content: str
    ) -> OperationResult:
        try:
            session = self._owned(current, handle)
            if session is None:
                return _error("invalid_conversation_handle")
            if session.lock.locked():
                return _error("conversation_busy")
            if self.executor.read_only:
                return _error("read_only_policy")
            async with session.lock:
                session.waiter = asyncio.current_task()
                try:
                    try:
                        prepared = await self._content(content)
                    except Exception:
                        return _error("invalid_message")
                    dismissed = await self._dismissed(current, session)
                    if dismissed:
                        return dismissed
                    history = await self._catchup(current, session)
                    self._principal(current, session.principal)
                    if history.error:
                        return history
                    cursor = history.data["next_cursor"] or session.last_seen
                    missed = history.data["messages"]
                    formatted = format_messages(missed, timezone=self.timezone)
                    if not history.data["exhausted"]:
                        session.last_seen = cursor
                        return _error(
                            "catchup_required",
                            {
                                "missed_messages": missed,
                                "formatted_messages": formatted,
                                "next_cursor": cursor,
                                "history_limited": history.data.get(
                                    "history_limited", False
                                ),
                            },
                        )
                    try:
                        sent = await self._call(
                            current,
                            session.principal,
                            "send-message",
                            {
                                "type": "stream",
                                "to": session.stream_id,
                                "topic": session.topic,
                                "content": prepared,
                            },
                        )
                    except asyncio.CancelledError:
                        # Zulip may have accepted the message before cancellation.
                        return OperationResult(
                            str(uuid4()),
                            error={"code": "conversation_cancelled"},
                            uncertain_write=True,
                        )
                    if sent.error:
                        return sent
                    mid = sent.data.get("id") if isinstance(sent.data, dict) else None
                    if not _positive(mid):
                        return OperationResult(
                            sent.request_id,
                            error={"code": "invalid_send_response"},
                            uncertain_write=True,
                        )
                    session.last_seen, session.last_sent = cursor, mid
                    warnings: list[str] = []
                    error = None
                    try:
                        try:
                            await self._hook(self.on_reply, mid, content)
                        except Exception:
                            warnings.append("on_reply_failed")
                        await self._typing(
                            current,
                            session,
                            "start" if self.auto_typing_start else "stop",
                            warnings,
                        )
                    except asyncio.CancelledError:
                        error = {"code": "conversation_cancelled"}
                    return OperationResult(
                        sent.request_id,
                        error=error,
                        data={
                            "sent_message_id": mid,
                            "missed_messages": missed,
                            "formatted_messages": formatted,
                            "history_limited": history.data.get(
                                "history_limited", False
                            ),
                            "context": session.snapshot(),
                            "warnings": warnings,
                        },
                    )
                finally:
                    session.waiter = None
        except AuthenticationError:
            return _error("authentication_required")
        except ValueError:
            return _error("invalid_conversation_response")

    async def _ensure_subscription(
        self,
        current: PrincipalProvider,
        session: _Conversation,
    ) -> OperationResult | None:
        result = await self._call(
            current,
            session.principal,
            "get-subscription-status",
            {
                "user_id": session.user_id,
                "stream_id": session.stream_id,
            },
        )
        if result.error:
            return result
        subscribed = (
            result.data.get("is_subscribed") if isinstance(result.data, dict) else None
        )
        if type(subscribed) is not bool:
            return _error("invalid_subscription_response")
        if subscribed:
            return None
        if session.private:
            return _error("private_channel_membership_required")
        result = await self._call(
            current,
            session.principal,
            "subscribe",
            {
                "subscriptions": [{"name": session.stream}],
            },
        )
        if result.error:
            return result
        # The subscription API accepts names, so detect a concurrent rename.
        check = await self._call(
            current, session.principal, "get-stream-id", {"stream": session.stream}
        )
        if (
            check.error
            or not isinstance(check.data, dict)
            or check.data.get("stream_id") != session.stream_id
        ):
            return OperationResult(
                result.request_id,
                error={"code": "subscription_target_changed"},
                uncertain_write=True,
            )
        return None

    async def _reaction_dismisses(
        self,
        current: PrincipalProvider,
        session: _Conversation,
        event: dict[str, Any],
    ) -> bool | OperationResult:
        if (
            event.get("op") not in ("add", "remove")
            or not _positive(event.get("message_id"))
            or not _positive(event.get("user_id"))
            or not isinstance(event.get("emoji_name"), str)
        ):
            return _error("invalid_reaction_event")
        if (
            event["op"] != "add"
            or event["user_id"] == session.user_id
            or event["emoji_name"] not in self.dismiss_emoji
        ):
            return False
        result = await self._call(
            current,
            session.principal,
            "get-message",
            {"message_id": event["message_id"], "apply_markdown": False},
        )
        if result.error:
            if result.error.get("code") == "channel_policy_denied":
                return False
            return result
        message = result.data.get("message") if isinstance(result.data, dict) else None
        if (
            not isinstance(message, dict)
            or not _positive(message.get("id"))
            or message["id"] != event["message_id"]
        ):
            return _error("invalid_reaction_target")
        # Queue narrows do not restrict reaction events to this topic.
        return (
            message.get("type") == "stream"
            and type(message.get("stream_id")) is int
            and message["stream_id"] == session.stream_id
            and isinstance(message.get("subject"), str)
            and message["subject"].casefold() == session.topic.casefold()
            and type(message.get("sender_id")) is int
            and message["sender_id"] == session.user_id
        )

    async def listen(
        self,
        current: PrincipalProvider,
        handle: str,
        *,
        timeout_seconds: float = 3600,
        progress: Callable[..., Any] | None = None,
    ) -> OperationResult:
        if (
            type(timeout_seconds) not in {float, int}
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 7200
        ):
            return _error("invalid_listen_timeout")
        try:
            session = self._owned(current, handle)
            if session is None:
                return _error("invalid_conversation_handle")
            if self.executor.read_only:
                return _error("read_only_policy")
            if session.lock.locked():
                return _error("conversation_busy")
            async with session.lock:
                session.waiter = asyncio.current_task()
                warnings: list[str] = []
                queue: str | None = None
                cursor = -1
                renewed = False
                history_limited = False
                indicator: dict[str, Any] | None = None
                started = monotonic()
                deadline = started + timeout_seconds
                try:
                    await self._typing(current, session, "stop", warnings)
                    dismissed = await self._dismissed(current, session)
                    if dismissed:
                        return dismissed
                    membership = await self._ensure_subscription(current, session)
                    if membership:
                        return membership
                    if session.last_seen:
                        emoji_name, reaction_type = "ear", "unicode_emoji"
                        emojis = await self._call(
                            current, session.principal, "get-custom-emoji", {}
                        )
                        values = (
                            emojis.data.get("emoji")
                            if isinstance(emojis.data, dict)
                            else None
                        )
                        if (
                            not emojis.error
                            and isinstance(values, dict)
                            and any(
                                isinstance(v, dict)
                                and v.get("name") == "robot_ear"
                                and v.get("deactivated") is False
                                for v in values.values()
                            )
                        ):
                            emoji_name, reaction_type = "robot_ear", "realm_emoji"
                        marker = {
                            "message_id": session.last_seen,
                            "emoji_name": emoji_name,
                            "reaction_type": reaction_type,
                        }
                        marked = await self._call(
                            current, session.principal, "add-reaction", marker
                        )
                        if not marked.error:
                            indicator = marker
                        else:
                            warnings.append("listen_indicator_failed")
                    while monotonic() < deadline:
                        self._principal(current, session.principal)
                        try:
                            interrupt = self._consume_interrupt()
                        except (OSError, ValueError):
                            return _error("interrupt_file_error")
                        if interrupt is not None:
                            return OperationResult(
                                str(uuid4()),
                                data={
                                    "interrupted": True,
                                    "interrupt_content": interrupt,
                                    "messages": [],
                                    "formatted_messages": "",
                                    "history_limited": history_limited,
                                    "resync_required": history_limited,
                                    "queue_renewed": renewed,
                                    "timed_out": False,
                                    "warnings": warnings,
                                },
                            )
                        if queue is None:
                            registered = await self.queues.register(
                                self._principal(current, session.principal),
                                {
                                    "event_types": ["message", "reaction"],
                                    "fetch_event_types": [],
                                    "narrow": [
                                        ["channel", session.stream],
                                        ["topic", session.topic],
                                    ],
                                    "apply_markdown": False,
                                },
                            )
                            if registered.error:
                                return registered
                            if (
                                not isinstance(registered.data, dict)
                                or not isinstance(registered.data.get("handle"), str)
                                or type(registered.data.get("last_event_id")) is not int
                            ):
                                return _error("invalid_queue_response")
                            queue, cursor = (
                                registered.data["handle"],
                                registered.data["last_event_id"],
                            )
                        # Register first; history then closes the registration gap.
                        history = await self._catchup(current, session)
                        if history.error:
                            return history
                        history_limited |= bool(
                            history.data.get("history_limited", False)
                        )
                        self._principal(current, session.principal)
                        formatted = format_messages(
                            history.data["messages"], timezone=self.timezone
                        )
                        session.last_seen = (
                            history.data["next_cursor"] or session.last_seen
                        )
                        if history.data["messages"] or not history.data["exhausted"]:
                            return OperationResult(
                                history.request_id,
                                data={
                                    "messages": history.data["messages"],
                                    "formatted_messages": formatted,
                                    "more_history": not history.data["exhausted"],
                                    "queue_renewed": renewed,
                                    "history_limited": history_limited,
                                    "resync_required": history_limited,
                                    "timed_out": False,
                                    "warnings": warnings,
                                },
                            )
                        dismissed = await self._dismissed(current, session)
                        if dismissed:
                            return dismissed
                        remaining = deadline - monotonic()
                        if remaining <= 0:
                            break
                        try:
                            async with asyncio.timeout(remaining):
                                events = await self.queues.poll(
                                    self._principal(current, session.principal),
                                    queue,
                                    cursor,
                                    wait_seconds=min(
                                        1 if self.interrupt_file is not None else 25,
                                        remaining,
                                    ),
                                )
                        except TimeoutError:
                            break
                        self._principal(current, session.principal)
                        if events.error:
                            if events.error.get("code") == "queue_expired":
                                queue, renewed = None, True
                                continue
                            return events
                        data = events.data
                        if (
                            not isinstance(data, dict)
                            or not isinstance(data.get("events"), list)
                            or type(data.get("next_event_id")) is not int
                            or data["next_event_id"] < cursor
                        ):
                            return _error("invalid_events_response")
                        visible: list[dict[str, Any]] = []
                        last_seen = session.last_seen
                        event_cursor = cursor
                        for event in data["events"]:
                            if (
                                not isinstance(event, dict)
                                or type(event.get("id")) is not int
                                or event["id"] <= event_cursor
                            ):
                                return _error("invalid_events_response")
                            event_cursor = event["id"]
                            if event.get("type") == "message":
                                message = event.get("message")
                                if not isinstance(message, dict):
                                    return _error("invalid_conversation_response")
                                visible.extend(
                                    self._messages(
                                        session, [message], exclude_self=True
                                    )
                                )
                                last_seen = max(last_seen, message["id"])
                            elif event.get("type") == "reaction":
                                dismissal = await self._reaction_dismisses(
                                    current, session, event
                                )
                                if isinstance(dismissal, OperationResult):
                                    return dismissal
                                if dismissal:
                                    session.dismissed = True
                                    return _error(
                                        "session_dismissed",
                                        {
                                            "emoji_name": event["emoji_name"],
                                            "warnings": warnings,
                                        },
                                    )
                            elif event.get("type") != "heartbeat":
                                return _error("unsupported_conversation_event")
                        if event_cursor > data["next_event_id"]:
                            return _error("invalid_events_response")
                        cursor = data["next_event_id"]
                        formatted = format_messages(visible, timezone=self.timezone)
                        session.last_seen = last_seen
                        if visible:
                            return OperationResult(
                                events.request_id,
                                data={
                                    "messages": visible,
                                    "formatted_messages": formatted,
                                    "more_history": False,
                                    "queue_renewed": renewed,
                                    "history_limited": history_limited,
                                    "resync_required": history_limited,
                                    "timed_out": False,
                                    "warnings": warnings,
                                },
                            )
                        await self._hook(
                            progress, monotonic() - started, timeout_seconds
                        )
                        if not data["events"] and not data.get("timed_out"):
                            await asyncio.sleep(
                                min(0.1, max(0, deadline - monotonic()))
                            )
                    return OperationResult(
                        str(uuid4()),
                        data={
                            "messages": [],
                            "formatted_messages": "",
                            "timed_out": True,
                            "queue_renewed": renewed,
                            "history_limited": history_limited,
                            "resync_required": history_limited,
                            "warnings": warnings,
                        },
                    )
                finally:
                    try:
                        async with asyncio.timeout(5):
                            if queue is not None:
                                cleaned = await self.queues.delete(
                                    session.principal, queue
                                )
                                if cleaned.error:
                                    warnings.append("queue_cleanup_failed")
                            if indicator:
                                cleaned = await self.executor.execute(
                                    session.principal, "remove-reaction", indicator
                                )
                                if cleaned.error:
                                    warnings.append("listen_indicator_cleanup_failed")
                            await self._typing(current, session, "stop", warnings)
                    except TimeoutError:
                        warnings.append("conversation_cleanup_timeout")
                    finally:
                        session.waiter = None
        except AuthenticationError:
            return _error("authentication_required")
        except ValueError:
            return _error("invalid_conversation_response")

    async def end(
        self,
        current: PrincipalProvider,
        handle: str,
        message: str = ":wave: Signing off",
    ) -> OperationResult:
        try:
            session = self._owned(current, handle)
            if session is None:
                return _error("invalid_conversation_handle")
            if session.waiter and session.waiter is not asyncio.current_task():
                session.waiter.cancel()
                with suppress(asyncio.CancelledError):
                    await session.waiter
            if session.lock.locked():
                return _error("conversation_busy")
            async with session.lock:
                warnings: list[str] = []
                self._write_exit_markers(session, warnings)
                farewell: OperationResult | None = None
                try:
                    if message and not session.dismissed:
                        seconds = int(monotonic() - session.started)
                        suffix = (
                            f" | {seconds}s" if seconds < 60 else f" | {seconds // 60}m"
                        )
                        try:
                            prefix = (
                                await self._hook(self.prefix)
                                if callable(self.prefix)
                                else self.prefix
                            )
                            if (
                                not isinstance(prefix, str)
                                or len(prefix) + len(suffix) > self.message_max_length
                            ):
                                raise ValueError("Prefix exceeds farewell budget")
                            body = normalize_zulip_markdown(prefix + message + suffix)
                            if len(body) > self.message_max_length:
                                body = (
                                    body[: self.message_max_length - len(suffix)]
                                    + suffix
                                )
                            body = prepare_message(
                                body, max_length=self.message_max_length
                            )
                        except Exception:
                            farewell = _error("invalid_message")
                        else:
                            farewell = await self._call(
                                current,
                                session.principal,
                                "send-message",
                                {
                                    "type": "stream",
                                    "to": session.stream_id,
                                    "topic": session.topic,
                                    "content": body,
                                },
                            )
                finally:
                    try:
                        await self._typing(current, session, "stop", warnings)
                        try:
                            callback = self.on_session_end
                            args: tuple[Any, ...] = (session.snapshot(),)
                            if callback:
                                try:
                                    inspect.signature(callback).bind(*args)
                                except TypeError:
                                    args = ()
                            await self._hook(callback, *args)
                        except Exception:
                            warnings.append("on_session_end_failed")
                    finally:
                        self._sessions.pop(handle, None)
                return OperationResult(
                    str(uuid4()),
                    data={
                        "ended": True,
                        "context": session.snapshot(),
                        "warnings": warnings,
                    },
                    error=farewell.error if farewell else None,
                    uncertain_write=farewell.uncertain_write if farewell else False,
                )
        except AuthenticationError:
            return _error("authentication_required")

    async def close(self) -> None:
        self._closed = True
        try:
            async with asyncio.timeout(5):
                for handle, session in list(self._sessions.items()):

                    def cached_principal(
                        principal: Principal = session.principal,
                    ) -> Principal:
                        return principal

                    await self.end(cached_principal, handle, "")
        except TimeoutError:
            pass
        finally:
            self._sessions.clear()
            if self._owns_queues:
                await self.queues.close()

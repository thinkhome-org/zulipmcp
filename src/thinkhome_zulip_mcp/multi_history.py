"""Bounded, resumable multi-channel history without dropping fetched messages."""

from collections.abc import Callable
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from thinkhome_zulip_mcp.executor import Executor, OperationResult, Principal
from thinkhome_zulip_mcp.formatting import context_window, format_messages
from thinkhome_zulip_mcp.search import construct_narrow, search_messages, time_bounds
from thinkhome_zulip_mcp.store import AuthenticationError


async def multi_channel_history(
    executor: Executor,
    current: Callable[[], Principal],
    *,
    channels: list[str | int] | None = None,
    sender: str | int | None = None,
    hours_back: float = 24,
    as_of: str | None = None,
    per_channel_limit: int = 100,
    max_pages: int = 10,
    cursors: dict[str, int | None] | None = None,
    include_formatted: bool = False,
    participant_id: int | None = None,
    timezone: str = "UTC",
) -> OperationResult:
    """Return all bounded channel samples sorted by ID; None selects public channels."""
    request_id = str(uuid4())
    try:
        ZoneInfo(timezone)
        if (
            type(include_formatted) is not bool
            or (
                participant_id is not None
                and (
                    type(participant_id) is not int
                    or participant_id < 1
                    or sender is not None
                )
            )
            or type(per_channel_limit) is not int
            or not 1 <= per_channel_limit <= 200
            or type(max_pages) is not int
            or not 1 <= max_pages <= 50
            or (
                channels is not None
                and (not isinstance(channels, list) or len(channels) > 20)
            )
        ):
            raise ValueError("Invalid history limits")
        narrows = (
            [[{"operator": "channels", "operand": "public"}]]
            if channels is None
            else [construct_narrow(channel=channel) for channel in channels]
        )
        if channels is not None and any(channel is None for channel in channels):
            raise ValueError("Invalid channel")
        construct_narrow(sender=sender)
        lower, upper = time_bounds(hours_back=hours_back, as_of=as_of)
        positions = {} if cursors is None else dict(cursors)
        if (
            not isinstance(cursors, (dict, type(None)))
            or set(positions) - {str(index) for index in range(len(narrows))}
            or any(
                value is not None and (type(value) is not int or value < 1)
                for value in positions.values()
            )
        ):
            raise ValueError("Invalid history cursors")
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        return OperationResult(
            request_id, error={"code": "invalid_multi_history_request"}
        )
    try:
        owner = current()

        def principal() -> Principal:
            active = current()
            if active != owner:
                raise AuthenticationError("History reader changed")
            return active

        messages: dict[int, dict[str, Any]] = {}
        errors = []
        history_limited = False
        for index, narrow in enumerate(narrows):
            key = str(index)
            if key in positions and positions[key] is None:
                continue
            result = await search_messages(
                executor,
                principal,
                narrow,
                sender=sender,
                limit=per_channel_limit,
                max_pages=max_pages,
                cursor=positions.get(key),
                after_time=lower,
                before_time=upper,
            )
            principal()
            if result.error:
                if result.error.get("code") == "authentication_required":
                    raise AuthenticationError("History authentication expired")
                errors.append({"channel_index": index, "error": result.error})
                continue
            for message in result.data["messages"]:
                messages.setdefault(message["id"], message)
            positions[key] = (
                None if result.data["exhausted"] else result.data["next_cursor"]
            )
            history_limited |= result.data["history_limited"]
        principal()
        ordered = sorted(messages.values(), key=lambda row: row["id"])
        context = None
        if participant_id is not None:
            groups: dict[tuple[int, str], list[dict[str, Any]]] = {}
            for message in ordered:
                if (
                    message.get("type") != "stream"
                    or type(message.get("stream_id")) is not int
                    or message["stream_id"] < 1
                    or not isinstance(message.get("subject"), str)
                    or type(message.get("timestamp")) is not int
                    or type(message.get("sender_id")) is not int
                    or message["sender_id"] < 1
                ):
                    raise ValueError("Invalid participant context message")
                key_topic = (message["stream_id"], message["subject"].casefold())
                groups.setdefault(key_topic, []).append(message)
            context = sorted(
                [
                    row
                    for group in groups.values()
                    for row in context_window(
                        sorted(group, key=lambda row: (row["timestamp"], row["id"])),
                        sender_id=participant_id,
                    )
                ],
                key=lambda row: row["id"],
            )
        formatted = (
            format_messages(
                context if context is not None else ordered,
                include_topic=True,
                timezone=timezone,
            )
            if include_formatted
            else None
        )
        return OperationResult(
            request_id,
            data={
                "messages": ordered,
                "context_messages": context,
                "context_scope": "retrieved_sample_per_topic"
                if participant_id is not None
                else None,
                "formatted": formatted,
                "channels": channels,
                "cursors": positions,
                "exhausted": not errors
                and all(
                    positions.get(str(index), False) is None
                    for index in range(len(narrows))
                ),
                "errors": errors,
                "partial": bool(errors),
                "history_limited": history_limited,
                "after_time": lower.isoformat() if lower else None,
                "as_of": upper.isoformat() if upper else None,
                "hours_back": hours_back,
            },
        )
    except AuthenticationError:
        return OperationResult(request_id, error={"code": "authentication_required"})

    except (ValueError, TypeError):
        return OperationResult(
            request_id, error={"code": "invalid_history_presentation"}
        )

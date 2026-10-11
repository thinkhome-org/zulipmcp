"""Search filters and caller-bound message discovery."""

import math
import re
from collections import Counter
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from thinkhome_zulip_mcp.executor import Executor, OperationResult, Principal
from thinkhome_zulip_mcp.formatting import format_messages
from thinkhome_zulip_mcp.store import AuthenticationError
from thinkhome_zulip_mcp.users import resolve_users
from thinkhome_zulip_mcp.workflows import history_page


def _identifier(value: Any) -> bool:
    return (type(value) is int and value > 0) or (
        isinstance(value, str) and bool(value.strip())
    )


def construct_narrow(
    *,
    narrow: list[dict[str, Any]] | None = None,
    channel: str | int | None = None,
    topic: str | None = None,
    sender: str | int | None = None,
    search_text: str | None = None,
    has_attachment: bool | None = None,
    has_link: bool | None = None,
    has_image: bool | None = None,
    is_private: bool | None = None,
    is_starred: bool | None = None,
    is_mentioned: bool | None = None,
    is_unread: bool | None = None,
    is_muted: bool | None = None,
    is_followed: bool | None = None,
    message_id: int | None = None,
    near_message_id: int | None = None,
    dm_with: str | list[int | str] | None = None,
    group_dm_with: str | list[int | str] | None = None,
) -> list[dict[str, Any]]:
    """Compose Zulip terms; false means negation, None means no added filter.

    Existing terms retain their server-defined meaning. Date bounds are handled
    by message paging, not disguised as full-text search expressions.
    """
    if narrow is not None and not isinstance(narrow, list):
        raise ValueError("narrow must be a list")
    result = deepcopy(narrow) if narrow is not None else []
    for term in result:
        if (
            not isinstance(term, dict)
            or not {"operator", "operand"} <= term.keys()
            or term.keys() - {"operator", "operand", "negated"}
            or not isinstance(term["operator"], str)
            or not term["operator"].strip()
            or ("negated" in term and type(term["negated"]) is not bool)
        ):
            raise ValueError("Invalid narrow term")
        operand = term["operand"]
        if not (
            isinstance(operand, str)
            or (type(operand) is int and operand > 0)
            or (
                isinstance(operand, list)
                and bool(operand)
                and all(_identifier(item) for item in operand)
            )
        ):
            raise ValueError("Invalid narrow operand")
    for operator, value in (("channel", channel), ("sender", sender)):
        if value is not None and not _identifier(value):
            raise ValueError(f"Invalid {operator}")
    # Keep topic ahead of sender/search for predictable generated filters.
    for operator, value in (
        ("channel", channel),
        ("topic", topic),
        ("sender", sender),
        ("search", search_text),
    ):
        if value is not None:
            if operator in {"topic", "search"} and not isinstance(value, str):
                raise ValueError(f"Invalid {operator}")
            result.append({"operator": operator, "operand": value})
    for operator, values in (
        ("has", {"attachment": has_attachment, "link": has_link, "image": has_image}),
        (
            "is",
            {
                "dm": is_private,
                "starred": is_starred,
                "mentioned": is_mentioned,
                "unread": is_unread,
                "muted": is_muted,
                "followed": is_followed,
            },
        ),
    ):
        for operand, enabled in values.items():
            if enabled is None:
                continue
            if type(enabled) is not bool:
                raise ValueError(f"Invalid {operator}:{operand} flag")
            term = {"operator": operator, "operand": operand}
            if not enabled:
                term["negated"] = True
            result.append(term)
    for operator, mid in (("id", message_id), ("near", near_message_id)):
        if mid is not None:
            if type(mid) is not int or mid < 1:
                raise ValueError(f"Invalid {operator} message ID")
            result.append({"operator": operator, "operand": mid})
    for operator, recipients in (("dm", dm_with), ("group-pm-with", group_dm_with)):
        if recipients is not None:
            if not (
                (isinstance(recipients, str) and recipients.strip())
                or (
                    isinstance(recipients, list)
                    and recipients
                    and all(_identifier(item) for item in recipients)
                )
            ):
                raise ValueError("Invalid direct-message recipients")
            result.append({"operator": operator, "operand": deepcopy(recipients)})
    return result


def time_bounds(
    *,
    after_time: str | datetime | None = None,
    before_time: str | datetime | None = None,
    as_of: str | datetime | None = None,
    last_hours: float | None = None,
    last_days: float | None = None,
    hours_back: float | None = None,
    now: datetime | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Intersect inclusive bounds in UTC; naive dates mean UTC.

    Relative windows end at as_of or now. Explicit before/after constraints
    intersect that window; they do not silently move its reference time.
    """

    def parse(value: str | datetime) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime):
            raise ValueError("Expected ISO date or datetime")
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    try:
        lower = parse(after_time) if after_time is not None else None
        upper = parse(before_time) if before_time is not None else None
        reference = (
            parse(as_of)
            if as_of is not None
            else parse(now)
            if now is not None
            else datetime.now(UTC)
        )
        if as_of is not None:
            upper = min(upper, reference) if upper is not None else reference
        for duration, scale in (
            (last_hours, 3600),
            (last_days, 86400),
            (hours_back, 3600),
        ):
            if duration is None:
                continue
            if (
                type(duration) not in (int, float)
                or not math.isfinite(duration)
                or duration <= 0
            ):
                raise ValueError("Relative intervals must be positive finite numbers")
            cutoff = reference - timedelta(seconds=duration * scale)
            lower = max(lower, cutoff) if lower is not None else cutoff
            upper = min(upper, reference) if upper is not None else reference
        if lower is not None and upper is not None and lower > upper:
            raise ValueError("Search time interval is empty")
        return lower, upper
    except (OverflowError, OSError) as exc:
        raise ValueError("Search date or interval is out of range") from exc


async def search_messages(
    executor: Executor,
    current: Callable[[], Principal],
    narrow: list[dict[str, Any]],
    *,
    sender: str | int | None = None,
    limit: int = 100,
    sort: str = "newest",
    cursor: int | None = None,
    max_pages: int = 10,
    include_summary: bool = False,
    include_formatted: bool = False,
    timezone: str = "UTC",
    after_time: str | datetime | None = None,
    before_time: str | datetime | None = None,
    as_of: str | datetime | None = None,
    last_hours: float | None = None,
    last_days: float | None = None,
    hours_back: float | None = None,
) -> OperationResult:
    """Fetch a bounded sample, preserving traversal through filtered-out rows."""
    request_id = str(uuid4())
    try:
        ZoneInfo(timezone)
        if (
            type(include_summary) is not bool
            or type(include_formatted) is not bool
            or type(limit) is not int
            or not 1 <= limit <= 1000
            or type(max_pages) is not int
            or not 1 <= max_pages <= 50
            or sort not in ("newest", "oldest", "relevance")
            or (cursor is not None and (type(cursor) is not int or cursor < 1))
        ):
            raise ValueError("Invalid search paging")
        if sender is not None and not _identifier(sender):
            raise ValueError("Invalid sender")
        narrow = construct_narrow(
            narrow=narrow, sender=sender if type(sender) is int else None
        )
        terms = {
            word
            for term in narrow
            if term["operator"] == "search"
            and not term.get("negated", False)
            and isinstance(term["operand"], str)
            for word in re.findall(r"\w+", term["operand"].casefold())
        }
        if sort == "relevance" and not terms:
            raise ValueError("Relevance requires positive search text")
        lower, upper = time_bounds(
            after_time=after_time,
            before_time=before_time,
            as_of=as_of,
            last_hours=last_hours,
            last_days=last_days,
            hours_back=hours_back,
        )
        minimum = lower.timestamp() if lower is not None else None
        maximum = upper.timestamp() if upper is not None else None
    except (ValueError, TypeError, ZoneInfoNotFoundError, OverflowError, OSError):
        return OperationResult(request_id, error={"code": "invalid_search_request"})
    try:
        owner = current()
        owner_key = (owner.identity_id, owner.realm_url.rstrip("/"))

        def principal() -> Principal:
            active = current()
            if (active.identity_id, active.realm_url.rstrip("/")) != owner_key:
                raise AuthenticationError("Search owner changed")
            return active

        if isinstance(sender, str):
            directory = await executor.execute(principal(), "get-users", {})
            principal()
            if directory.error:
                return directory
            try:
                resolved = resolve_users(
                    directory.data.get("members")
                    if isinstance(directory.data, dict)
                    else None,
                    sender,
                    include_bots=True,
                    include_inactive=True,
                )
            except ValueError:
                return OperationResult(
                    directory.request_id, error={"code": "invalid_user_response"}
                )
            if resolved["status"] != "resolved":
                return OperationResult(
                    directory.request_id,
                    data=resolved,
                    error={
                        "code": "ambiguous_sender"
                        if resolved["status"] == "ambiguous"
                        else "sender_not_found"
                    },
                )
            narrow.append(
                {"operator": "sender", "operand": resolved["matches"][0]["user_id"]}
            )
        messages: dict[int, dict[str, Any]] = {}
        exhausted = False
        history_limited = False
        date = lower if sort == "oldest" else upper
        if sort != "oldest" and date is not None:
            # Zulip anchors at the first match; include the entire boundary second.
            try:
                date += timedelta(seconds=1)
            except OverflowError:
                date = None
        pages = 0
        for _ in range(max_pages):
            page = await history_page(
                executor,
                principal(),
                narrow,
                cursor=cursor,
                direction="newer" if sort == "oldest" else "older",
                limit=min(200, limit - len(messages)),
                anchor_date=date.isoformat()
                if cursor is None and date is not None
                else None,
                apply_markdown=False,
            )
            principal()
            if page.error:
                return page
            request_id = page.request_id
            pages += 1
            data = page.data
            if type(data.get("history_limited", False)) is not bool:
                return OperationResult(
                    request_id, error={"code": "invalid_search_response"}
                )
            for message in data["messages"]:
                timestamp = message.get("timestamp")
                if (
                    type(timestamp) not in (int, float)
                    or not 0 <= timestamp < 253402300800  # UTC year 10000 boundary.
                ):
                    return OperationResult(
                        request_id, error={"code": "invalid_search_response"}
                    )
                if (minimum is None or timestamp >= minimum) and (
                    maximum is None or timestamp <= maximum
                ):
                    messages[message["id"]] = message
            cursor = data["next_cursor"]
            history_limited = history_limited or data.get("history_limited", False)
            exhausted = data["exhausted"]
            if exhausted or len(messages) >= limit:
                break
        scores: dict[str, int] = {}
        if sort == "relevance":
            for mid, message in messages.items():
                content, topic = message.get("content"), message.get("subject", "")
                if not isinstance(content, str) or not isinstance(topic, str):
                    return OperationResult(
                        request_id, error={"code": "invalid_search_response"}
                    )
                body_words = Counter(re.findall(r"\w+", content.casefold()))
                topic_words = Counter(re.findall(r"\w+", topic.casefold()))
                scores[str(mid)] = sum(
                    body_words[word] + 2 * topic_words[word] for word in terms
                )
        return OperationResult(
            request_id,
            data={
                "messages": sorted(
                    messages.values(),
                    key=lambda item: (
                        scores.get(str(item["id"]), 0),
                        item["timestamp"],
                        item["id"],
                    ),
                    reverse=sort != "oldest",
                ),
                "formatted": format_messages(
                    sorted(messages.values(), key=lambda row: row["id"]),
                    include_topic=True,
                    timezone=timezone,
                )
                if include_formatted
                else None,
                "next_cursor": cursor,
                "exhausted": exhausted,
                "has_more": not exhausted,
                "history_limited": history_limited,
                "pages_fetched": pages,
                "summary": summarize_search(list(messages.values()))
                if include_summary
                else None,
                "sort": sort,
                "ranking_scope": "retrieved_sample" if sort == "relevance" else None,
                "relevance_scores": scores if sort == "relevance" else None,
                "after_time": lower.isoformat() if lower is not None else None,
                "before_time": upper.isoformat() if upper is not None else None,
            },
        )
    except AuthenticationError:
        return OperationResult(request_id, error={"code": "authentication_required"})
    except ValueError:
        return OperationResult(request_id, error={"code": "invalid_search_response"})


async def check_matches(
    executor: Executor,
    current: Callable[[], Principal],
    message_ids: list[int],
    narrow: list[dict[str, Any]],
) -> OperationResult:
    """Check distinct supplied IDs; hidden matches count as non-matching."""
    request_id = str(uuid4())
    try:
        if (
            not isinstance(message_ids, list)
            or len(message_ids) > 1000
            or any(type(mid) is not int or mid < 1 for mid in message_ids)
        ):
            raise ValueError("Expected at most 1000 positive IDs")
        narrow = construct_narrow(narrow=narrow)
    except ValueError:
        return OperationResult(request_id, error={"code": "invalid_match_request"})
    try:
        owner = current()
        ids = list(dict.fromkeys(message_ids))
        result = await executor.execute(
            owner, "check-messages-match-narrow", {"msg_ids": ids, "narrow": narrow}
        )
        active = current()
        if (owner.identity_id, owner.realm_url.rstrip("/")) != (
            active.identity_id,
            active.realm_url.rstrip("/"),
        ):
            raise AuthenticationError("Match reader changed")
        if result.error:
            return result
        matches = result.data.get("messages") if isinstance(result.data, dict) else None
        requested = {str(mid) for mid in ids}
        if not isinstance(matches, dict) or any(
            key not in requested
            or not isinstance(value, dict)
            or any(
                not isinstance(value.get(field), str)
                for field in ("match_content", "match_subject")
            )
            for key, value in matches.items()
        ):
            return OperationResult(
                result.request_id, error={"code": "invalid_match_response"}
            )
        matching = [mid for mid in ids if str(mid) in matches]
        return OperationResult(
            result.request_id,
            data={
                "total_checked": len(ids),
                "matching_count": len(matching),
                "matching_message_ids": matching,
                "non_matching_count": len(ids) - len(matching),
                "messages": {
                    key: {
                        field: value[field]
                        for field in ("match_content", "match_subject")
                    }
                    for key, value in matches.items()
                },
            },
        )
    except AuthenticationError:
        return OperationResult(request_id, error={"code": "authentication_required"})


def summarize_search(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate only the supplied, distinct message sample."""
    if not isinstance(messages, list):
        raise ValueError("Expected message list")
    users: Counter[str] = Counter()
    channels: Counter[str] = Counter()
    topics: dict[tuple[int, str], dict[str, Any]] = {}
    seen: set[int] = set()
    direct = 0
    for message in messages:
        if not isinstance(message, dict) or any(
            type(message.get(field)) is not int or message[field] < 1
            for field in ("id", "sender_id")
        ):
            raise ValueError("Invalid message identity")
        mid = message["id"]
        if mid in seen:
            raise ValueError("Duplicate message in sample")
        seen.add(mid)
        users[str(message["sender_id"])] += 1
        if message.get("type") in ("private", "direct"):
            direct += 1
        elif message.get("type") == "stream":
            channel, topic = message.get("stream_id"), message.get("subject")
            if type(channel) is not int or channel < 1 or not isinstance(topic, str):
                raise ValueError("Invalid channel message")
            channels[str(channel)] += 1
            entry = topics.setdefault(
                (channel, topic.casefold()),
                {
                    "stream_id": channel,
                    "topic": topic,
                    "count": 0,
                    "latest_message_id": mid,
                },
            )
            entry["count"] += 1
            entry["latest_message_id"] = max(mid, entry["latest_message_id"])
        else:
            raise ValueError("Invalid message type")
    return {
        "scope": "returned_sample",
        "total_messages": len(messages),
        "count_by_user": dict(users),
        "count_by_stream": dict(channels),
        "direct_messages": direct,
        "topics": sorted(
            topics.values(),
            key=lambda item: (
                -item["latest_message_id"],
                item["stream_id"],
                item["topic"].casefold(),
            ),
        ),
    }


async def search_directory(
    executor: Executor,
    current: Callable[[], Principal],
    query: str,
    *,
    kind: str,
    limit: int = 100,
    offset: int = 0,
) -> OperationResult:
    """Search the current caller-visible user/channel directory, ordered by ID."""
    request_id = str(uuid4())
    if (
        kind not in ("users", "streams")
        or not isinstance(query, str)
        or type(limit) is not int
        or not 1 <= limit <= 1000
        or type(offset) is not int
        or offset < 0
    ):
        return OperationResult(request_id, error={"code": "invalid_directory_search"})
    try:
        owner = current()
        result = await executor.execute(
            owner, "get-users" if kind == "users" else "get-streams", {}
        )
        active = current()
        if (owner.identity_id, owner.realm_url.rstrip("/")) != (
            active.identity_id,
            active.realm_url.rstrip("/"),
        ):
            raise AuthenticationError("Directory reader changed")
        if result.error:
            return result
        request_id = result.request_id
        rows = (
            result.data.get("members" if kind == "users" else "streams")
            if isinstance(result.data, dict)
            else None
        )
        if not isinstance(rows, list):
            raise ValueError("Invalid directory")
        identifier = "user_id" if kind == "users" else "stream_id"
        fields = ("full_name", "email") if kind == "users" else ("name", "description")
        seen: set[int] = set()
        matches = []
        for row in rows:
            if (
                not isinstance(row, dict)
                or type(row.get(identifier)) is not int
                or row[identifier] < 1
                or row[identifier] in seen
                or any(not isinstance(row.get(field), str) for field in fields)
            ):
                raise ValueError("Invalid directory entry")
            seen.add(row[identifier])
            if any(query.casefold() in row[field].casefold() for field in fields):
                matches.append(row)
        matches.sort(key=lambda row: row[identifier])
        end = min(offset + limit, len(matches))
        return OperationResult(
            request_id,
            data={
                "kind": kind,
                "matches": matches[offset:end],
                "total_matches": len(matches),
                "next_offset": end if end < len(matches) else None,
                "exhausted": end >= len(matches),
                "scope": "caller_visible_directory",
            },
        )
    except AuthenticationError:
        return OperationResult(request_id, error={"code": "authentication_required"})
    except ValueError:
        return OperationResult(request_id, error={"code": "invalid_directory_response"})


async def search_topics(
    executor: Executor,
    current: Callable[[], Principal],
    query: str,
    stream_ids: list[int],
    *,
    limit: int = 100,
    cursor: dict[str, int] | None = None,
) -> OperationResult:
    """Search up to 20 channel topic lists, preserving partial failures and position."""
    request_id = str(uuid4())
    if (
        not isinstance(query, str)
        or type(limit) is not int
        or not 1 <= limit <= 1000
        or not isinstance(stream_ids, list)
        or len(stream_ids) > 20
        or any(type(mid) is not int or mid < 1 for mid in stream_ids)
        or len(set(stream_ids)) != len(stream_ids)
    ):
        return OperationResult(request_id, error={"code": "invalid_topic_search"})
    if cursor is None:
        cursor = {"channel_index": 0, "topic_offset": 0}
    if (
        not isinstance(cursor, dict)
        or set(cursor) != {"channel_index", "topic_offset"}
        or any(type(value) is not int or value < 0 for value in cursor.values())
        or cursor["channel_index"] > len(stream_ids)
        or (cursor["channel_index"] == len(stream_ids) and cursor["topic_offset"] != 0)
    ):
        return OperationResult(request_id, error={"code": "invalid_topic_cursor"})
    try:
        owner = current()
        owner_key = (owner.identity_id, owner.realm_url.rstrip("/"))

        def principal() -> Principal:
            active = current()
            if (active.identity_id, active.realm_url.rstrip("/")) != owner_key:
                raise AuthenticationError("Topic reader changed")
            return active

        found = []
        errors = []
        continuation = None
        for index in range(cursor["channel_index"], len(stream_ids)):
            channel = stream_ids[index]
            result = await executor.execute(
                principal(),
                "get-stream-topics",
                {
                    "stream_id": channel,
                    "allow_empty_topic_name": True,
                },
            )
            principal()
            if result.error:
                errors.append({"stream_id": channel, "error": result.error})
                continue
            topics = (
                result.data.get("topics") if isinstance(result.data, dict) else None
            )
            seen: set[str] = set()
            valid = isinstance(topics, list)
            if isinstance(topics, list):
                for topic in topics:
                    if (
                        not isinstance(topic, dict)
                        or not isinstance(topic.get("name"), str)
                        or type(topic.get("max_id")) is not int
                        or topic["max_id"] < 1
                        or topic["name"].casefold() in seen
                    ):
                        valid = False
                        break
                    seen.add(topic["name"].casefold())
            if not valid or not isinstance(topics, list):
                errors.append(
                    {"stream_id": channel, "error": {"code": "invalid_topic_response"}}
                )
                continue
            topics = sorted(topics, key=lambda topic: topic["name"].casefold())
            offset = cursor["topic_offset"] if index == cursor["channel_index"] else 0
            for position in range(offset, len(topics)):
                topic = topics[position]
                if query.casefold() in topic["name"].casefold():
                    found.append(
                        {
                            "stream_id": channel,
                            "name": topic["name"],
                            "max_id": topic["max_id"],
                        }
                    )
                if len(found) == limit:
                    if position + 1 < len(topics):
                        continuation = {
                            "channel_index": index,
                            "topic_offset": position + 1,
                        }
                    elif index + 1 < len(stream_ids):
                        continuation = {"channel_index": index + 1, "topic_offset": 0}
                    break
            if len(found) == limit:
                break
        return OperationResult(
            request_id,
            data={
                "topics": found,
                "errors": errors,
                "partial": bool(errors),
                "next_cursor": continuation,
                "exhausted": continuation is None,
                "stream_ids": list(stream_ids),
                "scope": "caller_visible_channel_topics",
            },
        )
    except AuthenticationError:
        return OperationResult(request_id, error={"code": "authentication_required"})


async def advanced_search(
    executor: Executor,
    current: Callable[[], Principal],
    query: str,
    *,
    sections: list[str] | None = None,
    limit: int = 100,
    cursors: dict[str, Any] | None = None,
    stream_ids: list[int] | None = None,
) -> OperationResult:
    """Compose bounded searches; all sections belong to one authenticated reader."""
    request_id = str(uuid4())
    sections = ["messages"] if sections is None else sections
    cursors = {} if cursors is None else cursors
    allowed = {"messages", "users", "streams", "topics"}
    if (
        not isinstance(query, str)
        or type(limit) is not int
        or not 1 <= limit <= 1000
        or not isinstance(sections, list)
        or not sections
        or any(
            not isinstance(section, str) or section not in allowed
            for section in sections
        )
        or len(set(sections)) != len(sections)
        or not isinstance(cursors, dict)
        or set(cursors) - set(sections)
        or (
            stream_ids is not None
            and (
                not isinstance(stream_ids, list)
                or len(stream_ids) > 20
                or any(type(mid) is not int or mid < 1 for mid in stream_ids)
                or len(set(stream_ids)) != len(stream_ids)
            )
        )
    ):
        return OperationResult(request_id, error={"code": "invalid_advanced_search"})
    for section, cursor in cursors.items():
        if section != "topics" and (
            type(cursor) is not int or cursor < (1 if section == "messages" else 0)
        ):
            return OperationResult(request_id, error={"code": "invalid_search_cursor"})
        if section == "topics" and (
            not isinstance(cursor, dict)
            or set(cursor) != {"directory_offset", "channel_index", "topic_offset"}
            or any(type(value) is not int or value < 0 for value in cursor.values())
            or (stream_ids is not None and cursor["directory_offset"] != 0)
        ):
            return OperationResult(request_id, error={"code": "invalid_topic_cursor"})
    try:
        owner = current()
        owner_key = (owner.identity_id, owner.realm_url.rstrip("/"))

        def principal() -> Principal:
            active = current()
            if (active.identity_id, active.realm_url.rstrip("/")) != owner_key:
                raise AuthenticationError("Search reader changed")
            return active

        results: dict[str, Any] = {}
        for section in sections:
            principal()
            if section == "messages":
                result = await search_messages(
                    executor,
                    principal,
                    construct_narrow(search_text=query),
                    limit=limit,
                    cursor=cursors.get(section),
                    include_summary=True,
                )
            elif section in ("users", "streams"):
                result = await search_directory(
                    executor,
                    principal,
                    query,
                    kind=section,
                    limit=limit,
                    offset=cursors.get(section, 0),
                )
            else:
                topic_cursor = cursors.get(
                    section,
                    {"directory_offset": 0, "channel_index": 0, "topic_offset": 0},
                )
                directory_offset = topic_cursor["directory_offset"]
                next_directory = None
                channels = stream_ids
                if channels is None:
                    directory = await search_directory(
                        executor,
                        principal,
                        "",
                        kind="streams",
                        limit=20,
                        offset=directory_offset,
                    )
                    principal()
                    if directory.error:
                        if directory.error.get("code") == "authentication_required":
                            raise AuthenticationError("Search authentication expired")
                        results[section] = {"data": None, "error": directory.error}
                        continue
                    channels = [row["stream_id"] for row in directory.data["matches"]]
                    next_directory = directory.data["next_offset"]
                result = await search_topics(
                    executor,
                    principal,
                    query,
                    channels,
                    limit=limit,
                    cursor={
                        key: topic_cursor[key]
                        for key in ("channel_index", "topic_offset")
                    },
                )
                if result.data is not None:
                    continuation = result.data["next_cursor"]
                    if continuation is not None:
                        continuation = {
                            "directory_offset": directory_offset,
                            **continuation,
                        }
                    elif next_directory is not None:
                        continuation = {
                            "directory_offset": next_directory,
                            "channel_index": 0,
                            "topic_offset": 0,
                        }
                    result.data["next_cursor"] = continuation
                    result.data["exhausted"] = continuation is None
            principal()
            if result.error and result.error.get("code") == "authentication_required":
                raise AuthenticationError("Search authentication expired")
            results[section] = {"data": result.data, "error": result.error}
        principal()
        return OperationResult(
            request_id,
            data={
                "sections": results,
                "partial": any(
                    row["error"] or (row["data"] and row["data"].get("partial"))
                    for row in results.values()
                ),
            },
        )
    except AuthenticationError:
        return OperationResult(request_id, error={"code": "authentication_required"})

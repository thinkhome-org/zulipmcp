"""Caller-bound channel discovery and optional membership/topic reads."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from thinkhome_zulip_mcp.executor import Executor, OperationResult, Principal
from thinkhome_zulip_mcp.store import AuthenticationError


async def channel_info(
    executor: Executor,
    current: Callable[[], Principal],
    *,
    name: str | None = None,
    stream_id: int | None = None,
    include_subscribers: bool = False,
    include_member_profiles: bool = False,
    include_topics: bool = False,
    topic_limit: int = 100,
    topic_offset: int = 0,
) -> OperationResult:
    """Read channel metadata with explicit optional errors and topic continuation."""
    request_id = str(uuid4())
    if (
        (name is None and stream_id is None)
        or (name is not None and (not isinstance(name, str) or not name.strip()))
        or (stream_id is not None and (type(stream_id) is not int or stream_id < 1))
        or type(include_subscribers) is not bool
        or type(include_member_profiles) is not bool
        or type(include_topics) is not bool
        or type(topic_limit) is not int
        or not 1 <= topic_limit <= 1000
        or type(topic_offset) is not int
        or topic_offset < 0
    ):
        return OperationResult(request_id, error={"code": "invalid_channel_request"})
    try:
        owner = current()

        def principal() -> Principal:
            active = current()
            if active != owner:
                raise AuthenticationError("Channel reader changed")
            return active

        if name is not None:
            resolved = await executor.execute(
                principal(), "get-stream-id", {"stream": name}
            )
            principal()
            if resolved.error:
                return resolved
            found = (
                resolved.data.get("stream_id")
                if isinstance(resolved.data, dict)
                else None
            )
            if type(found) is not int or found < 1:
                return OperationResult(
                    request_id, error={"code": "invalid_channel_response"}
                )
            if stream_id is not None and found != stream_id:
                return OperationResult(
                    request_id, error={"code": "channel_identity_mismatch"}
                )
            stream_id = found
        result = await executor.execute(
            principal(), "get-stream-by-id", {"stream_id": stream_id}
        )
        principal()
        if result.error:
            return result
        stream = result.data.get("stream") if isinstance(result.data, dict) else None
        if (
            not isinstance(stream, dict)
            or type(stream.get("stream_id")) is not int
            or stream["stream_id"] != stream_id
            or not isinstance(stream.get("name"), str)
        ):
            return OperationResult(
                request_id, error={"code": "invalid_channel_response"}
            )
        data: dict[str, Any] = {"stream": stream, "errors": [], "partial": False}
        errors = []
        for section, operation, enabled in (
            (
                "subscribers",
                "get-subscribers",
                include_subscribers or include_member_profiles,
            ),
            ("topics", "get-stream-topics", include_topics),
        ):
            if not enabled:
                continue
            arguments = {"stream_id": stream_id}
            if section == "topics":
                arguments["allow_empty_topic_name"] = True
            extra = await executor.execute(principal(), operation, arguments)
            principal()
            if extra.error:
                if extra.error.get("code") == "authentication_required":
                    raise AuthenticationError("Channel authentication expired")
                errors.append({"section": section, "error": extra.error})
                continue
            rows = extra.data.get(section) if isinstance(extra.data, dict) else None
            valid = isinstance(rows, list)
            if isinstance(rows, list) and section == "subscribers":
                valid = all(type(mid) is int and mid > 0 for mid in rows)
                valid = valid and len(set(rows)) == len(rows)
            elif isinstance(rows, list):
                valid = all(
                    isinstance(row, dict)
                    and isinstance(row.get("name"), str)
                    and type(row.get("max_id")) is int
                    and row["max_id"] > 0
                    for row in rows
                )
                valid = valid and len({row["name"].casefold() for row in rows}) == len(
                    rows
                )
            if not valid or not isinstance(rows, list):
                errors.append(
                    {"section": section, "error": {"code": "invalid_channel_response"}}
                )
                continue
            if section == "subscribers":
                data[section] = sorted(rows)
                data["subscriber_count"] = len(rows)
            else:
                rows = sorted(
                    rows, key=lambda row: (-row["max_id"], row["name"].casefold())
                )
                data[section] = rows[topic_offset : topic_offset + topic_limit]
                data["topic_count"] = len(rows)
                end = topic_offset + topic_limit
                data["next_topic_offset"] = end if end < len(rows) else None
        if include_member_profiles and "subscribers" in data:
            directory = await executor.execute(principal(), "get-users", {})
            principal()
            if directory.error:
                if directory.error.get("code") == "authentication_required":
                    raise AuthenticationError("Channel authentication expired")
                errors.append({"section": "member_profiles", "error": directory.error})
            else:
                members = (
                    directory.data.get("members")
                    if isinstance(directory.data, dict)
                    else None
                )
                if (
                    not isinstance(members, list)
                    or any(
                        not isinstance(member, dict)
                        or type(member.get("user_id")) is not int
                        or member["user_id"] < 1
                        or not isinstance(member.get("full_name"), str)
                        for member in members
                    )
                    or len({member["user_id"] for member in members}) != len(members)
                ):
                    errors.append(
                        {
                            "section": "member_profiles",
                            "error": {"code": "invalid_channel_response"},
                        }
                    )
                else:
                    by_id = {member["user_id"]: member for member in members}
                    data["member_profiles"] = [
                        by_id[mid] for mid in data["subscribers"] if mid in by_id
                    ]
                    data["unresolved_subscriber_ids"] = [
                        mid for mid in data["subscribers"] if mid not in by_id
                    ]
        principal()
        data["errors"] = errors
        data["partial"] = bool(errors)
        return OperationResult(request_id, data=data)
    except AuthenticationError:
        return OperationResult(request_id, error={"code": "authentication_required"})


async def list_channels(
    executor: Executor,
    current: Callable[[], Principal],
    *,
    include_public: bool = True,
    include_subscribed: bool = True,
    include_web_public: bool = False,
    exclude_archived: bool = True,
    include_all: bool = False,
    include_all_active: bool = False,
    include_default: bool = False,
    include_owner_subscribed: bool = False,
    include_can_access_content: bool = False,
    limit: int = 100,
    offset: int = 0,
    fields: list[str] | None = None,
) -> OperationResult:
    """Page the caller-visible directory using upstream inclusion semantics."""
    request_id = str(uuid4())
    arguments = {
        "include_public": include_public,
        "include_subscribed": include_subscribed,
        "include_web_public": include_web_public,
        "exclude_archived": exclude_archived,
        "include_all": include_all,
        "include_all_active": include_all_active,
        "include_default": include_default,
        "include_owner_subscribed": include_owner_subscribed,
        "include_can_access_content": include_can_access_content,
    }
    if (
        any(type(value) is not bool for value in arguments.values())
        or type(limit) is not int
        or not 1 <= limit <= 1000
        or type(offset) is not int
        or offset < 0
        or (
            fields is not None
            and (
                not isinstance(fields, list)
                or len(fields) > 100
                or any(
                    not isinstance(field, str) or not field or len(field) > 200
                    for field in fields
                )
            )
        )
    ):
        return OperationResult(request_id, error={"code": "invalid_channel_request"})
    try:
        owner = current()
        result = await executor.execute(owner, "get-streams", arguments)
        active = current()
        if active != owner:
            raise AuthenticationError("Channel reader changed")
        if result.error:
            return result
        rows = result.data.get("streams") if isinstance(result.data, dict) else None
        if not isinstance(rows, list) or any(
            not isinstance(row, dict)
            or type(row.get("stream_id")) is not int
            or row["stream_id"] < 1
            or not isinstance(row.get("name"), str)
            for row in rows
        ):
            return OperationResult(
                request_id, error={"code": "invalid_channel_response"}
            )
        if len({row["stream_id"] for row in rows}) != len(rows):
            return OperationResult(
                request_id, error={"code": "invalid_channel_response"}
            )
        rows = sorted(rows, key=lambda row: row["stream_id"])
        end = offset + limit
        return OperationResult(
            result.request_id,
            data={
                "streams": rows[offset:end],
                "formatted": _format_channels(rows[offset:end], fields or []),
                "total_count": len(rows),
                "next_offset": end if end < len(rows) else None,
                "exhausted": end >= len(rows),
                "scope": "caller_visible_directory",
            },
        )
    except AuthenticationError:
        return OperationResult(request_id, error={"code": "authentication_required"})


def _format_channels(rows: list[dict[str, Any]], fields: list[str]) -> str:
    """Render presentation fields without changing the original channel records."""
    lines = []
    for row in rows:
        visibility = "private" if row.get("invite_only") else "public"
        line = f"- [{visibility}] #{row['name']}"
        if row.get("description"):
            line += f": {row['description']}"
        values = []
        for field in fields:
            value = row.get(field)
            if (
                field == "date_created"
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            ):
                try:
                    value = datetime.fromtimestamp(value, UTC).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
                except (ValueError, OverflowError, OSError):
                    pass
            if isinstance(value, (dict, list)):
                value = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
                if len(value) > 80:
                    value = value[:77] + "..."
            values.append(f"{field}={value}")
        if values:
            line += " | " + " ".join(values)
        lines.append(line)
    return "\n".join(lines) if lines else "No channels found in this page."

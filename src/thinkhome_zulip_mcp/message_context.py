"""Caller-authorized message context, stable links and source attribution."""

import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from thinkhome_zulip_mcp.executor import Executor, OperationResult, Principal
from thinkhome_zulip_mcp.formatting import format_messages
from thinkhome_zulip_mcp.store import AuthenticationError
from thinkhome_zulip_mcp.workflows import history_page


async def message_context(
    executor: Executor, current: Callable[[], Principal], message_id: int
) -> OperationResult:
    request_id = str(uuid4())
    if type(message_id) is not int or message_id < 1:
        return OperationResult(request_id, error={"code": "invalid_message_id"})
    try:
        owner = current()
        result = await executor.execute(
            owner,
            "get-message",
            {
                "message_id": message_id,
                "apply_markdown": False,
                "allow_empty_topic_name": True,
            },
        )
        active = current()
        if (active.identity_id, active.realm_url.rstrip("/")) != (
            owner.identity_id,
            owner.realm_url.rstrip("/"),
        ):
            raise AuthenticationError("Message reader changed")
        if result.error:
            return result
        request_id = result.request_id
        message = result.data.get("message") if isinstance(result.data, dict) else None
        if (
            not isinstance(message, dict)
            or type(message.get("id")) is not int
            or message["id"] != message_id
            or not isinstance(message.get("content"), str)
        ):
            raise ValueError("Invalid message")
        context: dict[str, Any] = {"type": message.get("type")}
        fragment = f"#narrow/id/{message_id}"
        label = f"message {message_id}"
        if message.get("type") == "stream":
            channel_id, topic, channel = (
                message.get("stream_id"),
                message.get("subject"),
                message.get("display_recipient"),
            )
            if (
                type(channel_id) is not int
                or channel_id < 1
                or not isinstance(topic, str)
                or not isinstance(channel, str)
            ):
                raise ValueError("Invalid channel message")
            encoded = quote(topic, safe="").replace(".", ".2E").replace("%", ".")
            fragment = f"#narrow/channel/{channel_id}/topic/{encoded}/near/{message_id}"
            context.update(stream_id=channel_id, stream=channel, topic=topic)
            label = f"#{channel} > {topic}"
        elif message.get("type") not in {"private", "direct"}:
            raise ValueError("Invalid message type")
        url = owner.realm_url.rstrip("/") + "/" + fragment
        label = re.sub(
            r"([\\`*_[\]<>])",
            lambda match: chr(92) + match.group(),
            " ".join(label.split()),
        )
        return OperationResult(
            request_id,
            data={
                "message": message,
                "context": context,
                "url": url,
                "markdown_link": f"[{label}]({url})",
                "verified_message": json.dumps(
                    {"source": "zulip_api", "message": message},
                    ensure_ascii=False,
                    allow_nan=False,
                ),
                "verification": "API source attribution; not a cryptographic signature",
            },
        )
    except AuthenticationError:
        return OperationResult(request_id, error={"code": "authentication_required"})
    except (ValueError, TypeError):
        return OperationResult(request_id, error={"code": "invalid_message_response"})


def _conversation_narrow(message: dict[str, Any]) -> list[dict[str, Any]]:
    if message.get("type") == "stream":
        channel, topic = message.get("stream_id"), message.get("subject")
        if type(channel) is not int or channel < 1 or not isinstance(topic, str):
            raise ValueError("Invalid channel context")
        return [
            {"operator": "channel", "operand": channel},
            {"operator": "topic", "operand": topic.casefold()},
        ]
    if message.get("type") not in {"private", "direct"}:
        raise ValueError("Invalid conversation type")
    recipients = message.get("display_recipient")
    if not isinstance(recipients, list) or not recipients:
        raise ValueError("Missing DM recipients")
    ids = []
    for user in recipients:
        uid = user.get("id") if isinstance(user, dict) else None
        if type(uid) is not int or uid < 1 or uid in ids:
            raise ValueError("Invalid DM recipients")
        ids.append(uid)
    return [{"operator": "dm", "operand": sorted(ids)}]


async def nearby_messages(
    executor: Executor,
    current: Callable[[], Principal],
    message_id: int,
    *,
    before: int = 20,
    after: int = 20,
) -> OperationResult:
    """Read at most two bounded pages in the anchor's conversation."""
    request_id = str(uuid4())
    if any(
        type(count) is not int or not 0 <= count <= 100 for count in (before, after)
    ):
        return OperationResult(request_id, error={"code": "invalid_context_request"})
    try:
        owner = current()

        def principal() -> Principal:
            active = current()
            if (active.identity_id, active.realm_url.rstrip("/")) != (
                owner.identity_id,
                owner.realm_url.rstrip("/"),
            ):
                raise AuthenticationError("Context reader changed")
            return active

        anchor = await message_context(executor, principal, message_id)
        if anchor.error:
            return anchor
        request_id = anchor.request_id
        target = anchor.data["message"]
        narrow = _conversation_narrow(target)
        messages = [target]
        metadata: dict[str, Any] = {
            "before_cursor": message_id,
            "after_cursor": message_id,
            "found_oldest": None,
            "found_newest": None,
            "history_limited": False,
        }
        for count, direction, prefix, flag in (
            (before, "older", "before", "found_oldest"),
            (after, "newer", "after", "found_newest"),
        ):
            if not count:
                continue
            page = await history_page(
                executor,
                principal(),
                narrow,
                cursor=message_id,
                direction=direction,
                limit=count,
                apply_markdown=False,
            )
            principal()
            if page.error:
                return page
            if type(page.data.get("history_limited", False)) is not bool:
                raise ValueError("Invalid history restriction flag")
            for message in page.data["messages"]:
                if _conversation_narrow(message) != narrow:
                    raise ValueError("Message outside anchor conversation")
            messages.extend(page.data["messages"])
            metadata[prefix + "_cursor"] = page.data["next_cursor"]
            metadata[flag] = page.data["exhausted"]
            metadata["history_limited"] |= page.data.get("history_limited", False)
        messages.sort(key=lambda message: message["id"])
        return OperationResult(
            request_id,
            data={
                **anchor.data,
                **metadata,
                "messages": messages,
                "formatted_messages": format_messages(messages),
            },
        )
    except AuthenticationError:
        return OperationResult(request_id, error={"code": "authentication_required"})
    except (ValueError, TypeError):
        return OperationResult(request_id, error={"code": "invalid_context_response"})

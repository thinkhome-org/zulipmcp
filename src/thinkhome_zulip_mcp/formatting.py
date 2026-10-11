"""Pure formatting of caller-authorized messages; never fetches additional data."""

import re
from collections import Counter
from datetime import datetime
from typing import Any

# This module emits escaped XML; it never parses caller XML.
from xml.etree.ElementTree import Element, tostring  # nosec B405
from zoneinfo import ZoneInfo

_XML_INVALID = re.compile(
    r"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]"
)


def _xml_text(value: Any) -> str:
    if not isinstance(value, str) or _XML_INVALID.search(value):
        raise ValueError("Invalid XML text")
    return value


def summarize_reactions(reactions: list[dict[str, Any]]) -> str:
    if not isinstance(reactions, list) or any(
        not isinstance(r, dict) for r in reactions
    ):
        raise ValueError("Invalid reactions")
    counts: Counter[str] = Counter()
    for reaction in reactions:
        name = reaction.get("emoji_name")
        if isinstance(name, str) and name:
            counts[_xml_text(name)] += 1
    return "  ".join(
        f":{name}:" + (f" x{count}" if count > 1 else "")
        for name, count in counts.items()
    )


def format_messages(
    messages: list[dict[str, Any]],
    include_topic: bool = False,
    combine: bool = True,
    *,
    timezone: str = "UTC",
) -> str:
    zone = ZoneInfo(timezone)
    groups: list[dict[str, Any]] = []
    for source in messages:
        if not isinstance(source, dict):
            raise ValueError("Invalid message")
        if any(
            type(source.get(key)) is not int or source[key] <= 0
            for key in ("id", "sender_id")
        ):
            raise ValueError("Message and sender IDs must be positive integers")
        content, topic = source.get("content"), source.get("subject", "")
        timestamp = source.get("timestamp", 0)
        if (
            not isinstance(content, str)
            or not isinstance(topic, str)
            or type(timestamp) is not int
            or timestamp < 0
        ):
            raise ValueError("Invalid message content, topic or timestamp")
        _xml_text(content)
        _xml_text(topic)
        _xml_text(source.get("sender_full_name", "Unknown"))
        if source.get("type") == "stream":
            _xml_text(source.get("display_recipient", ""))
        summarize_reactions(source.get("reactions", []))
        try:
            datetime.fromtimestamp(timestamp, zone)
        except (OverflowError, OSError) as exc:
            raise ValueError("Invalid message timestamp") from exc
        if any(
            word in topic.casefold() or content.strip().casefold().startswith(word)
            for word in ("/nb", "/nobots")
        ):
            continue
        current = dict(source)
        current.pop("end", None)
        current["timestamp"] = timestamp
        current["reactions"] = list(source.get("reactions", []))
        previous = groups[-1] if groups else None
        if (
            combine
            and previous is not None
            and source["sender_id"] == previous["sender_id"]
            and source.get("type") == "stream" == previous.get("type")
            and type(source.get("stream_id")) is int
            and source["stream_id"] == previous.get("stream_id")
            and topic.casefold() == previous.get("subject", "").casefold()
            and 0 <= timestamp - previous.get("end", previous["timestamp"]) <= 120
        ):
            previous["content"] += "\n\n" + content
            previous["end"] = timestamp
            previous["id"] = source["id"]
            previous["reactions"].extend(current["reactions"])
        else:
            groups.append(current)

    output = []
    last: datetime | None = None
    for message in groups:
        moment = datetime.fromtimestamp(message["timestamp"], zone)
        attrs = {"user": str(message.get("sender_full_name", "Unknown"))}
        stamp = ""
        end = datetime.fromtimestamp(message.get("end", message["timestamp"]), zone)
        if "end" in message:
            end_format = (
                "%H:%M:%S %Z" if moment.date() == end.date() else "%Y-%m-%d %H:%M:%S %Z"
            )
            stamp = (
                moment.strftime("%Y-%m-%d %H:%M:%S %Z") + "-" + end.strftime(end_format)
            )
        elif last is None or moment.date() != last.date():
            stamp = moment.strftime("%Y-%m-%d %H:%M:%S %Z")
        elif moment.timestamp() - last.timestamp() >= 300:
            stamp = moment.strftime("%H:%M:%S %Z")
        if stamp:
            attrs["time"] = stamp
        if include_topic and message.get("type") == "stream":
            attrs["stream"] = str(message.get("display_recipient", ""))
            attrs["topic"] = message.get("subject", "")
        attrs["id"] = str(message["id"])
        reactions = summarize_reactions(message["reactions"])
        if reactions:
            attrs["reactions"] = reactions
        element = Element("msg", attrs)
        element.text = "\n" + message["content"] + "\n"
        output.append(tostring(element, encoding="unicode"))
        last = end
    return "\n".join(output)


def format_topics(
    messages: list[dict[str, Any]], combine: bool = True, *, timezone: str = "UTC"
) -> str:
    """Group channel history by immutable channel ID and case-insensitive topic."""
    # The parser receives only our own escaped formatter output.
    from xml.etree.ElementTree import fromstring  # nosec B405

    groups: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for message in messages:
        channel, topic = message.get("stream_id"), message.get("subject")
        if (
            message.get("type") != "stream"
            or type(channel) is not int
            or channel <= 0
            or not isinstance(topic, str)
        ):
            raise ValueError("Topic grouping requires channel messages")
        groups.setdefault((channel, topic.casefold()), []).append(message)
    output = []
    for (channel, _), group in sorted(
        groups.items(), key=lambda item: max(m["id"] for m in item[1])
    ):
        body = format_messages(group, combine=combine, timezone=timezone)
        if not body:
            continue
        section = Element(
            "topic",
            {
                "stream_id": str(channel),
                "stream": str(group[0].get("display_recipient", "")),
                "topic": group[0]["subject"],
            },
        )
        # Only format_messages output enters the parser; message XML is escaped text.
        section.extend(fromstring("<root>" + body + "</root>"))  # nosec B314
        output.append(tostring(section, encoding="unicode"))
    return "\n".join(output)


def context_window(
    messages: list[dict[str, Any]], *, sender_id: int
) -> list[dict[str, Any]]:
    """Keep reference-sized history windows around an identified participant."""
    if type(sender_id) is not int or sender_id <= 0:
        raise ValueError("Sender ID must be positive")
    timestamps: list[int] = []
    for message in messages:
        timestamp = message.get("timestamp")
        if type(timestamp) is not int or timestamp < 0:
            raise ValueError("Context requires chronological integer timestamps")
        timestamps.append(timestamp)
    if any(
        older > newer for older, newer in zip(timestamps, timestamps[1:], strict=False)
    ):
        raise ValueError("Context requires chronological integer timestamps")
    keep: set[int] = set()
    for index, message in enumerate(messages):
        if message.get("sender_id") != sender_id:
            continue
        keep.add(index)
        for before in range(index - 1, -1, -1):
            if index - before > 3 and timestamps[index] - timestamps[before] > 600:
                break
            keep.add(before)
        for after in range(index + 1, len(messages)):
            distance = after - index
            if distance > 3 and (
                distance >= 10 or timestamps[after] - timestamps[index] > 7200
            ):
                break
            keep.add(after)
    return [messages[index] for index in sorted(keep)]

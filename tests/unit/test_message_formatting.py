import copy
import xml.etree.ElementTree as ET

import pytest

from thinkhome_zulip_mcp.formatting import format_messages, summarize_reactions


def msg(mid, **changes):
    return {
        "id": mid,
        "sender_id": 2,
        "sender_full_name": 'A "<&',
        "type": "stream",
        "stream_id": 7,
        "display_recipient": 'Work "<&',
        "subject": 'Topic "<&',
        "timestamp": 0,
        "content": "<msg> & text",
        "reactions": [],
        **changes,
    }


def parse(value):
    return ET.fromstring("<root>" + value + "</root>")


def test_formatting_escapes_content_and_attributes_and_does_not_mutate():
    messages = [msg(1), msg(2, timestamp=120)]
    before = copy.deepcopy(messages)
    root = parse(
        format_messages(messages, include_topic=True, timezone="Europe/Prague")
    )
    assert len(root) == 1
    assert root[0].attrib["user"] == 'A "<&'
    assert root[0].attrib["topic"] == 'Topic "<&'
    assert root[0].attrib["id"] == "2"
    assert "CET" in root[0].attrib["time"]
    assert root[0].text.strip() == "<msg> & text\n\n<msg> & text"
    assert messages == before


def test_only_same_identity_channel_topic_and_forward_time_can_combine():
    base = msg(1, timestamp=200)
    for changes in (
        {"sender_id": 3},
        {"stream_id": 8},
        {"subject": "else"},
        {"timestamp": 199},
        {"timestamp": 321},
    ):
        other = msg(2, timestamp=201)
        other.update(changes)
        assert len(parse(format_messages([base, other]))) == 2
    assert (
        len(parse(format_messages([base, msg(2, timestamp=201)], combine=False))) == 2
    )


def test_smart_timestamps_filtering_and_reactions():
    messages = [
        msg(1),
        msg(2, timestamp=60, sender_id=3),
        msg(3, timestamp=360, sender_id=4),
        msg(4, content=" /nb hidden"),
        msg(5, subject="/nobots"),
    ]
    root = parse(format_messages(messages))
    assert len(root) == 3
    assert root[0].attrib["time"] == "1970-01-01 00:00:00 UTC"
    assert "time" not in root[1].attrib
    assert root[2].attrib["time"] == "00:06:00 UTC"
    assert (
        summarize_reactions(
            [{"emoji_name": "ok"}, {"emoji_name": "ok"}, {"emoji_name": "wave"}]
        )
        == ":ok: x2  :wave:"
    )


def test_topics_group_by_channel_identity_and_sort_by_latest_id():
    from thinkhome_zulip_mcp.formatting import format_topics

    value = format_topics(
        [
            msg(1, stream_id=7),
            msg(2, stream_id=8),
            msg(3, stream_id=7),
        ]
    )
    root = parse(value)
    assert [group.attrib["stream_id"] for group in root] == ["8", "7"]
    assert root[0].attrib["topic"] == 'Topic "<&'
    assert root[1][0].attrib["id"] == "3"


def test_participant_window_keeps_reference_before_after_bounds():
    from thinkhome_zulip_mcp.formatting import context_window

    messages = [
        msg(i + 1, timestamp=i * 60, sender_id=2 if i == 12 else 3) for i in range(30)
    ]
    assert [m["id"] for m in context_window(messages, sender_id=2)] == list(
        range(3, 23)
    )
    assert context_window(messages, sender_id=99) == []
    sparse = [
        msg(i + 1, timestamp=i * 10000, sender_id=2 if i == 5 else 3) for i in range(15)
    ]
    assert [m["id"] for m in context_window(sparse, sender_id=2)] == list(range(3, 10))


@pytest.mark.parametrize(
    "changes",
    [
        {"content": "bad\x00"},
        {"sender_full_name": "bad\ud800"},
        {"subject": "bad\ufffe"},
        {"display_recipient": "bad\x01"},
        {"reactions": None},
        {"reactions": [None]},
        {"reactions": [{"emoji_name": "bad\x02"}]},
        {"timestamp": 10**100},
        {"sender_full_name": []},
    ],
)
def test_malformed_metadata_raises_value_error(changes):
    with pytest.raises(ValueError):
        format_messages([msg(1, **changes)], include_topic=True)


def test_upstream_end_field_cannot_inject_a_time_range():
    root = parse(format_messages([msg(1, end=999999)]))
    assert root[0].attrib["time"] == "1970-01-01 00:00:00 UTC"

"""Wire evidence for each channel creation/update parameter, without live writes."""

import json
from urllib.parse import parse_qs

import httpx
import pytest

from thinkhome_zulip_mcp.executor import Executor, Principal
from thinkhome_zulip_mcp.server import create_server

GROUPS = (
    "can_add_subscribers_group",
    "can_remove_subscribers_group",
    "can_administer_channel_group",
    "can_delete_any_message_group",
    "can_delete_own_message_group",
    "can_move_messages_out_of_channel_group",
    "can_move_messages_within_channel_group",
    "can_send_message_group",
    "can_subscribe_group",
    "can_resolve_topics_group",
    "can_create_topic_group",
)
TOPICS = ["inherit", "allow_empty_topic", "disable_empty_topic", "empty_topic_only"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["subscribe", "update-stream"])
async def test_every_channel_parameter_reaches_wire(operation):
    group = {"direct_members": [22], "direct_subgroups": [3]}
    common = {
        "is_web_public": [False, True],
        "history_public_to_subscribers": [False, True],
        "is_default_stream": [False, True],
        "message_retention_days": [30, "realm_default", "forever"],
        "topics_policy": TOPICS,
        "folder_id": [1],
    }
    if operation == "subscribe":
        variants = {
            **common,
            "subscriptions": [
                [{"name": "eng", "description": "Work", "color": "#abc"}]
            ],
            "principals": [[22], ["member@example.test"]],
            "authorization_errors_fatal": [False, True],
            "announce": [False, True],
            "invite_only": [False, True],
            "send_new_subscription_messages": [False, True],
            **{key: [3, group] for key in GROUPS},
        }
    else:
        variants = {
            **common,
            "folder_id": [1, None],
            "description": ["", "Work"],
            "new_name": ["Engineering"],
            "is_private": [False, True],
            "is_archived": [False, True],
            **{key: [{"new": 3}, {"new": group, "old": 4}] for key in GROUPS},
        }
    seen = []
    expected = {}

    def upstream(request):
        assert request.method == ("POST" if operation == "subscribe" else "PATCH")
        assert request.url.path == (
            "/api/v1/users/me/subscriptions"
            if operation == "subscribe"
            else "/api/v1/streams/7"
        )
        assert (
            request.headers["authorization"]
            == httpx.BasicAuth("a", "synthetic")._auth_header
        )
        assert parse_qs(request.content.decode(), keep_blank_values=True) == expected
        seen.append(request)
        return httpx.Response(200, json={"result": "success"})

    executor = Executor(transport=httpx.MockTransport(upstream))
    content = executor.catalog[operation].definition["requestBody"]["content"][
        "application/x-www-form-urlencoded"
    ]
    assert set(variants) == set(content["schema"]["properties"])
    server = create_server(
        Principal("a", "https://zulip.example", "a", "synthetic"), executor
    )
    for field, values in variants.items():
        for value in values:
            encoded = (
                field in GROUPS
                or field == "folder_id"
                or (
                    operation == "subscribe"
                    and field in {"subscriptions", "principals", "topics_policy"}
                )
            )
            wire = (
                json.dumps(value, separators=(",", ":"))
                if encoded
                else (str(value).lower() if isinstance(value, bool) else str(value))
            )
            expected = {field: [wire]}
            if operation == "subscribe":
                subscriptions = value if field == "subscriptions" else [{"name": "eng"}]
                args = {"subscriptions": subscriptions, "options": {}}
                expected["subscriptions"] = [
                    json.dumps(subscriptions, separators=(",", ":"))
                ]
                if field == "principals":
                    args["principals"] = value
                elif field != "subscriptions":
                    args["options"][field] = value
                name = "zulip_subscribe"
            else:
                args = {"stream_id": 7, "settings": {field: value}}
                name = "zulip_update_channel"
            result = await server.call_tool(name, args)
            assert not result.is_error, (field, value, result.structured_content)
    assert len(seen) == sum(len(values) for values in variants.values())

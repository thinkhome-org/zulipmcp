import httpx
import pytest

from thinkhome_zulip_mcp.channels import channel_info, list_channels
from thinkhome_zulip_mcp.executor import Executor, Principal


@pytest.mark.asyncio
async def test_channel_directory_wire_preserves_requested_filters():
    def upstream(request):
        assert request.url.path == "/api/v1/streams"
        assert request.url.params["include_public"] == "false"
        assert request.url.params["include_subscribed"] == "true"
        assert request.url.params["include_can_access_content"] == "true"
        assert request.url.params["exclude_archived"] == "false"
        assert (
            request.headers["authorization"]
            == httpx.BasicAuth("alice", "synthetic")._auth_header
        )
        return httpx.Response(
            200,
            json={
                "result": "success",
                "streams": [{"stream_id": 7, "name": "eng", "invite_only": False}],
            },
        )

    executor = Executor(transport=httpx.MockTransport(upstream), read_only=True)
    caller = Principal("alice", "https://zulip.example", "alice", "synthetic")
    result = await list_channels(
        executor,
        lambda: caller,
        include_public=False,
        include_can_access_content=True,
        exclude_archived=False,
    )
    assert result.error is None
    assert result.data["streams"][0]["stream_id"] == 7


@pytest.mark.asyncio
async def test_channel_info_wire_resolves_then_fetches_optional_sections():
    seen = []

    def upstream(request):
        seen.append(request.url.path)
        if request.url.path == "/api/v1/get_stream_id":
            assert request.url.params["stream"] == "eng"
            data = {"stream_id": 7}
        elif request.url.path == "/api/v1/streams/7":
            data = {"stream": {"stream_id": 7, "name": "eng"}}
        elif request.url.path == "/api/v1/streams/7/members":
            data = {"subscribers": [1, 2]}
        else:
            assert request.url.path == "/api/v1/users/me/7/topics"
            assert request.url.params["allow_empty_topic_name"] == "true"
            data = {"topics": [{"name": "", "max_id": 10}]}
        return httpx.Response(200, json={"result": "success", **data})

    executor = Executor(transport=httpx.MockTransport(upstream), read_only=True)
    caller = Principal("alice", "https://zulip.example", "alice", "synthetic")
    result = await channel_info(
        executor,
        lambda: caller,
        name="eng",
        include_subscribers=True,
        include_topics=True,
    )
    assert result.error is None
    assert result.data["partial"] is False
    assert len(seen) == 4
    assert result.data["topics"][0]["name"] == ""


@pytest.mark.asyncio
async def test_channel_tools_registered_and_callable_in_read_only_server():
    from thinkhome_zulip_mcp.server import create_server

    def upstream(request):
        if request.url.path == "/api/v1/streams":
            return httpx.Response(200, json={"result": "success", "streams": []})
        assert request.url.path == "/api/v1/streams/7"
        return httpx.Response(
            200, json={"result": "success", "stream": {"stream_id": 7, "name": "eng"}}
        )

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream), read_only=True),
    )
    for name, args in (
        ("zulip_list_channels", {}),
        ("zulip_get_channel", {"stream_id": 7}),
    ):
        result = await server.call_tool(name, args)
        assert not result.is_error
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert tools["zulip_list_channels"].annotations.read_only_hint is True
    assert tools["zulip_get_channel"].annotations.read_only_hint is True


@pytest.mark.asyncio
async def test_subscription_setting_tool_validates_then_writes_once():
    from urllib.parse import parse_qs

    from thinkhome_zulip_mcp.server import create_server

    calls = []

    def upstream(request):
        calls.append(request)
        assert request.method == "PATCH"
        assert request.url.path == "/api/v1/users/me/subscriptions/7"
        assert parse_qs(request.content.decode()) == {
            "property": ["color"],
            "value": ["#abcdef"],
        }
        return httpx.Response(200, json={"result": "success"})

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream)),
    )
    bad = await server.call_tool(
        "zulip_update_subscription_property",
        {
            "stream_id": 7,
            "property": "is_muted",
            "value": "false",
        },
    )
    assert bad.is_error
    assert calls == []
    good = await server.call_tool(
        "zulip_update_subscription_property",
        {
            "stream_id": 7,
            "property": "color",
            "value": "#abcdef",
        },
    )
    assert not good.is_error
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_bulk_subscription_tool_rejects_bad_value_before_any_write():
    from thinkhome_zulip_mcp.server import create_server

    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(200, json={"result": "success"})

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream)),
    )
    result = await server.call_tool(
        "zulip_update_subscription_settings",
        {
            "subscription_data": [
                {"stream_id": 7, "property": "is_muted", "value": True},
                {"stream_id": 8, "property": "pin_to_top", "value": "true"},
            ],
        },
    )
    assert result.is_error
    assert calls == []
    result = await server.call_tool(
        "zulip_update_subscription_settings",
        {
            "subscription_data": [
                {"stream_id": 7, "property": "is_muted", "value": True}
            ],
        },
    )
    assert not result.is_error
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("principals", [[22], ["bob@example.org"]])
async def test_unsubscribe_tool_sends_explicit_principals(principals):
    import json
    from urllib.parse import parse_qs

    from thinkhome_zulip_mcp.server import create_server

    def upstream(request):
        assert request.method == "DELETE"
        form = parse_qs(request.content.decode())
        assert json.loads(form["subscriptions"][0]) == ["eng"]
        assert json.loads(form["principals"][0]) == principals
        return httpx.Response(200, json={"result": "success"})

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream)),
    )
    result = await server.call_tool(
        "zulip_unsubscribe", {"subscriptions": ["eng"], "principals": principals}
    )
    assert not result.is_error


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["true", "false", "partial"])
async def test_subscription_read_tool_preserves_settings(mode):
    from thinkhome_zulip_mcp.server import create_server

    def upstream(request):
        assert request.method == "GET"
        assert request.url.path == "/api/v1/users/me/subscriptions"
        assert request.url.params["include_subscribers"] == mode
        return httpx.Response(
            200,
            json={
                "result": "success",
                "subscriptions": [
                    {
                        "stream_id": 7,
                        "name": "eng",
                        "color": "#abc",
                        "is_muted": True,
                        "subscribers": [1, 2],
                    },
                ],
            },
        )

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream), read_only=True),
    )
    result = await server.call_tool(
        "zulip_get_subscriptions", {"include_subscribers": mode}
    )
    assert not result.is_error
    subscription = result.structured_content["data"]["subscriptions"][0]
    assert subscription["color"] == "#abc"
    assert subscription["is_muted"] is True
    assert subscription["subscribers"] == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,arguments,method,path",
    [
        ("zulip_archive_channel", {"stream_id": 7}, "DELETE", "/api/v1/streams/7"),
        (
            "zulip_delete_topic",
            {"stream_id": 7, "topic_name": ""},
            "POST",
            "/api/v1/streams/7/delete_topic",
        ),
    ],
)
async def test_destructive_channel_tools_wire_and_annotation(
    tool, arguments, method, path
):
    from thinkhome_zulip_mcp.server import create_server

    def upstream(request):
        assert request.method == method
        assert request.url.path == path
        return httpx.Response(200, json={"result": "success", "complete": False})

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream)),
    )
    result = await server.call_tool(tool, arguments)
    assert not result.is_error
    assert result.structured_content["data"]["complete"] is False
    definitions = {item.name: item for item in await server.list_tools()}
    assert definitions[tool].annotations.destructive_hint is True


@pytest.mark.asyncio
async def test_update_channel_tool_preserves_nullable_settings_and_fixed_target():
    from urllib.parse import parse_qs

    from thinkhome_zulip_mcp.server import create_server

    calls = []

    def upstream(request):
        calls.append(request)
        assert request.method == "PATCH"
        assert request.url.path == "/api/v1/streams/7"
        body = parse_qs(request.content.decode())
        assert body["description"] == ["Updated"]
        assert body["folder_id"] == ["null"]
        return httpx.Response(200, json={"result": "success"})

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream)),
    )
    rejected = await server.call_tool(
        "zulip_update_channel", {"stream_id": 7, "settings": {"stream_id": 8}}
    )
    assert rejected.is_error
    assert calls == []
    result = await server.call_tool(
        "zulip_update_channel",
        {"stream_id": 7, "settings": {"description": "Updated", "folder_id": None}},
    )
    assert not result.is_error
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_subscribe_tool_preserves_options_without_target_override():
    import json
    from urllib.parse import parse_qs

    from thinkhome_zulip_mcp.server import create_server

    calls = []

    def upstream(request):
        calls.append(request)
        assert request.method == "POST"
        assert request.url.path == "/api/v1/users/me/subscriptions"
        body = parse_qs(request.content.decode())
        assert json.loads(body["subscriptions"][0]) == [
            {"name": "eng", "description": "Work"}
        ]
        assert json.loads(body["principals"][0]) == [22]
        assert body["invite_only"] == ["true"]
        return httpx.Response(
            200, json={"result": "success", "subscribed": {"22": ["eng"]}}
        )

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream)),
    )
    bad = await server.call_tool(
        "zulip_subscribe",
        {
            "subscriptions": [{"name": "eng"}],
            "options": {"subscriptions": [{"name": "other"}]},
        },
    )
    assert bad.is_error
    assert calls == []
    result = await server.call_tool(
        "zulip_subscribe",
        {
            "subscriptions": [{"name": "eng", "description": "Work"}],
            "principals": [22],
            "options": {"invite_only": True},
        },
    )
    assert not result.is_error
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_channel_tool_resolves_member_profiles_with_callers_directory():
    from thinkhome_zulip_mcp.server import create_server

    calls = []

    def upstream(request):
        calls.append(request.url.path)
        assert (
            request.headers["authorization"]
            == httpx.BasicAuth("alice", "synthetic")._auth_header
        )
        if request.url.path.endswith("/streams/7/members"):
            payload = {"subscribers": [22, 23]}
        elif request.url.path.endswith("/streams/7"):
            payload = {"stream": {"stream_id": 7, "name": "eng"}}
        else:
            assert request.url.path == "/api/v1/users"
            payload = {
                "members": [{"user_id": 22, "full_name": "Alice", "is_active": False}]
            }
        return httpx.Response(200, json={"result": "success", **payload})

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream)),
    )
    result = await server.call_tool(
        "zulip_get_channel", {"stream_id": 7, "include_member_profiles": True}
    )
    assert not result.is_error
    payload = result.structured_content
    assert payload["data"]["unresolved_subscriber_ids"] == [23]
    assert payload["data"]["member_profiles"][0]["is_active"] is False
    assert len(calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("visibility", [0, 1, 2, 3])
@pytest.mark.parametrize("allowed", [True, False])
async def test_topic_visibility_tool_and_policy(visibility, allowed):
    from urllib.parse import parse_qs

    from thinkhome_zulip_mcp.channel_policy import ChannelPolicy
    from thinkhome_zulip_mcp.server import create_server

    writes = []

    def upstream(request):
        if request.method == "GET":
            assert request.url.path == "/api/v1/streams/7"
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "stream": {"stream_id": 7, "name": "eng", "invite_only": False},
                },
            )
        assert request.url.path == "/api/v1/user_topics"
        assert request.method == "POST"
        writes.append(parse_qs(request.content.decode(), keep_blank_values=True))
        return httpx.Response(200, json={"result": "success"})

    executor = Executor(
        channel_policy=ChannelPolicy(
            write_allowlist=frozenset({"eng"} if allowed else {"other"})
        ),
        transport=httpx.MockTransport(upstream),
    )
    server = create_server(
        Principal("a", "https://zulip.example", "a", "synthetic"), executor
    )
    result = await server.call_tool(
        "zulip_set_topic_visibility",
        {"stream_id": 7, "topic": "", "visibility_policy": visibility},
    )
    assert result.is_error is (not allowed)
    assert writes == (
        [{"stream_id": ["7"], "topic": [""], "visibility_policy": [str(visibility)]}]
        if allowed
        else []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("allowed", [True, False])
async def test_default_channel_tool_enforces_policy(enabled, allowed):
    from urllib.parse import parse_qs

    from thinkhome_zulip_mcp.channel_policy import ChannelPolicy
    from thinkhome_zulip_mcp.server import create_server

    writes = []

    def upstream(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "stream": {"stream_id": 7, "name": "eng", "invite_only": False},
                },
            )
        assert request.url.path == "/api/v1/default_streams"
        assert request.method == ("POST" if enabled else "DELETE")
        assert parse_qs(request.content.decode()) == {"stream_id": ["7"]}
        writes.append(request)
        return httpx.Response(200, json={"result": "success"})

    server = create_server(
        Principal("a", "https://zulip.example", "a", "synthetic"),
        Executor(
            channel_policy=ChannelPolicy(
                write_allowlist=frozenset({"eng"} if allowed else {"other"})
            ),
            transport=httpx.MockTransport(upstream),
        ),
    )
    result = await server.call_tool(
        "zulip_set_default_channel", {"stream_id": 7, "enabled": enabled}
    )
    assert result.is_error is (not allowed)
    assert len(writes) == int(allowed)


@pytest.mark.asyncio
@pytest.mark.parametrize("topic", [None, "", "work"])
@pytest.mark.parametrize("allowed", [True, False])
async def test_mark_channel_read_preserves_empty_topic_and_policy(topic, allowed):
    from urllib.parse import parse_qs

    from thinkhome_zulip_mcp.channel_policy import ChannelPolicy
    from thinkhome_zulip_mcp.server import create_server

    writes = []

    def upstream(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "stream": {"stream_id": 7, "name": "eng", "invite_only": False},
                },
            )
        assert request.method == "POST"
        assert request.url.path == (
            "/api/v1/mark_stream_as_read"
            if topic is None
            else "/api/v1/mark_topic_as_read"
        )
        body = parse_qs(request.content.decode(), keep_blank_values=True)
        assert body == (
            {"stream_id": ["7"]}
            if topic is None
            else {"stream_id": ["7"], "topic_name": [topic]}
        )
        writes.append(request)
        return httpx.Response(200, json={"result": "success"})

    server = create_server(
        Principal("a", "https://zulip.example", "a", "synthetic"),
        Executor(
            channel_policy=ChannelPolicy(
                write_allowlist=frozenset({"eng"} if allowed else {"other"})
            ),
            transport=httpx.MockTransport(upstream),
        ),
    )
    result = await server.call_tool(
        "zulip_mark_channel_read", {"stream_id": 7, "topic": topic}
    )
    assert result.is_error is (not allowed)
    assert len(writes) == int(allowed)


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [True, False])
@pytest.mark.parametrize(
    "flag,op,anchor",
    [
        ("read", "add", "first_unread"),
        ("read", "remove", 12),
        ("starred", "add", "newest"),
    ],
)
async def test_modern_channel_flags_preserve_partial_range(allowed, flag, op, anchor):
    import json
    from urllib.parse import parse_qs

    from thinkhome_zulip_mcp.channel_policy import ChannelPolicy
    from thinkhome_zulip_mcp.server import create_server

    writes = []
    response = {
        "result": "success",
        "processed_count": 2,
        "updated_count": 1,
        "first_processed_id": 12,
        "last_processed_id": 13,
        "found_oldest": True,
        "found_newest": False,
    }

    def upstream(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "stream": {"stream_id": 7, "name": "eng", "invite_only": False},
                },
            )
        assert request.method == "POST"
        assert request.url.path == "/api/v1/messages/flags/narrow"
        body = parse_qs(request.content.decode())
        assert json.loads(body["narrow"][0]) == [
            {"operator": "channel", "operand": 7},
            {"operator": "topic", "operand": ""},
        ]
        assert body["flag"] == [flag]
        assert body["op"] == [op]
        assert body["anchor"] == [str(anchor)]
        assert body["num_before"] == ["0"]
        assert body["num_after"] == ["1000"]
        assert body["include_anchor"] == ["true"]
        writes.append(request)
        return httpx.Response(200, json=response)

    server = create_server(
        Principal("a", "https://zulip.example", "a", "synthetic"),
        Executor(
            channel_policy=ChannelPolicy(
                write_allowlist=frozenset({"eng"} if allowed else {"other"})
            ),
            transport=httpx.MockTransport(upstream),
        ),
    )
    result = await server.call_tool(
        "zulip_update_channel_message_flags",
        {"stream_id": 7, "topic": "", "flag": flag, "op": op, "anchor": anchor},
    )
    assert result.is_error is (not allowed)
    assert len(writes) == int(allowed)
    if allowed:
        assert result.structured_content["data"] == response


@pytest.mark.asyncio
async def test_channel_list_tool_formats_fields_locally():
    from thinkhome_zulip_mcp.server import create_server

    def upstream(request):
        assert "fields" not in request.url.params
        return httpx.Response(
            200,
            json={
                "result": "success",
                "streams": [
                    {
                        "stream_id": 7,
                        "name": "eng",
                        "date_created": 0,
                        "extra": "retained",
                    }
                ],
            },
        )

    server = create_server(
        Principal("a", "https://zulip.example", "a", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream), read_only=True),
    )
    result = await server.call_tool("zulip_list_channels", {"fields": ["date_created"]})
    assert not result.is_error
    assert "1970-01-01T00:00:00Z" in result.structured_content["data"]["formatted"]
    assert result.structured_content["data"]["streams"][0]["extra"] == "retained"


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [True, False])
async def test_subscription_status_tool_readonly_and_visibility(allowed):
    from thinkhome_zulip_mcp.channel_policy import ChannelPolicy
    from thinkhome_zulip_mcp.server import create_server

    checks = []

    def upstream(request):
        assert request.method == "GET"
        if request.url.path == "/api/v1/streams/7":
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "stream": {
                        "stream_id": 7,
                        "name": "30 Work" if allowed else "general",
                        "invite_only": False,
                    },
                },
            )
        assert request.url.path == "/api/v1/users/22/subscriptions/7"
        checks.append(request)
        return httpx.Response(200, json={"result": "success", "is_subscribed": False})

    server = create_server(
        Principal("a", "https://zulip.example", "a", "synthetic"),
        Executor(
            read_only=True,
            channel_policy=ChannelPolicy(jd_enabled=True),
            transport=httpx.MockTransport(upstream),
        ),
    )
    result = await server.call_tool(
        "zulip_get_subscription_status", {"user_id": 22, "stream_id": 7}
    )
    assert result.is_error is (not allowed)
    assert len(checks) == int(allowed)
    if allowed:
        assert result.structured_content["data"]["is_subscribed"] is False

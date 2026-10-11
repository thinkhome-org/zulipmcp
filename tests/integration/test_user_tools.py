from urllib.parse import unquote

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from thinkhome_zulip_mcp.channel_policy import ChannelPolicy
from thinkhome_zulip_mcp.executor import Executor, Principal
from thinkhome_zulip_mcp.server import create_server


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool, arguments, path, payload",
    [
        (
            "zulip_get_users",
            {"user_ids": [7, 9], "include_custom_profile_fields": True},
            "/api/v1/users",
            {"members": []},
        ),
        ("zulip_get_user", {"user_id": 7}, "/api/v1/users/7", {"user": {"user_id": 7}}),
        (
            "zulip_get_user",
            {"email": "alice+work@example.org"},
            "/api/v1/users/alice+work@example.org",
            {"user": {"user_id": 7}},
        ),
        ("zulip_get_own_user", {}, "/api/v1/users/me", {"user_id": 7}),
    ],
)
async def test_user_tools_use_shared_executor_and_caller(
    tool, arguments, path, payload
):
    calls = []

    def upstream(request):
        calls.append(request)
        assert request.method == "GET"
        assert unquote(request.url.path) == path
        assert (
            request.headers["authorization"]
            == httpx.BasicAuth("alice", "synthetic")._auth_header
        )
        if tool == "zulip_get_users":
            assert request.url.params["user_ids"] == "[7,9]"
            assert request.url.params["include_custom_profile_fields"] == "true"
        return httpx.Response(200, json={"result": "success", **payload})

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(
            channel_policy=ChannelPolicy(jd_enabled=True),
            transport=httpx.MockTransport(upstream),
        ),
    )
    result = await server.call_tool(tool, arguments)
    assert not result.is_error
    assert result.structured_content["data"] == {"result": "success", **payload}
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments", [{}, {"user_id": 7, "email": "alice@example"}, {"user_id": -1}]
)
async def test_user_selector_rejects_ambiguous_or_missing_target(arguments):
    def upstream(request):
        pytest.fail("Invalid target must not reach Zulip")

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(
            channel_policy=ChannelPolicy(jd_enabled=True),
            transport=httpx.MockTransport(upstream),
        ),
    )
    with pytest.raises(ToolError):
        await server.call_tool("zulip_get_user", arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize("field_status", [200, 403])
async def test_profile_values_include_field_definitions_or_explicit_partial_error(
    field_status,
):
    def upstream(request):
        if request.url.path == "/api/v1/users/7":
            assert request.url.params["include_custom_profile_fields"] == "true"
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "user": {"user_id": 7, "profile_data": {"1": {"value": "123"}}},
                },
            )
        assert request.url.path == "/api/v1/realm/profile_fields"
        return httpx.Response(
            field_status,
            json={
                "result": "success" if field_status == 200 else "error",
                "custom_fields": [{"id": 1, "name": "Phone", "type": 1}],
            },
        )

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(
            channel_policy=ChannelPolicy(jd_enabled=True),
            transport=httpx.MockTransport(upstream),
        ),
    )
    result = await server.call_tool(
        "zulip_get_user", {"user_id": 7, "include_custom_profile_fields": True}
    )
    assert result.is_error == (field_status != 200)
    assert (
        result.structured_content["data"]["user"]["profile_data"]["1"]["value"] == "123"
    )
    if field_status == 200:
        assert result.structured_content["data"]["custom_fields"][0]["name"] == "Phone"
    else:
        assert (
            result.structured_content["error"]["code"] == "profile_fields_unavailable"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"members": [{"user_id": 7, "full_name": "Alice", "email": "alice@example"}]},
        {"members": "invalid"},
    ],
)
async def test_resolve_user_is_read_only_and_validates_directory(payload):
    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(
            read_only=True,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"result": "success", **payload}
                )
            ),
        ),
    )
    result = await server.call_tool("zulip_resolve_user", {"query": "Alice"})
    assert result.is_error == isinstance(payload["members"], str)
    if not result.is_error:
        assert result.structured_content["data"]["status"] == "resolved"
        assert result.structured_content["data"]["matches"][0]["user_id"] == 7
    else:
        assert result.structured_content["data"] is None
    tool = next(
        tool for tool in await server.list_tools() if tool.name == "zulip_resolve_user"
    )
    assert tool.annotations.read_only_hint is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool, arguments, method, path, encoded",
    [
        ("zulip_get_presence", {}, "GET", "/api/v1/realm/presence", {}),
        (
            "zulip_get_presence",
            {"user_id_or_email": 7},
            "GET",
            "/api/v1/users/7/presence",
            {},
        ),
        (
            "zulip_get_presence",
            {"user_id_or_email": "alice@example"},
            "GET",
            "/api/v1/users/alice@example/presence",
            {},
        ),
        ("zulip_get_user_status", {"user_id": 7}, "GET", "/api/v1/users/7/status", {}),
        (
            "zulip_get_user_groups",
            {"include_deactivated_groups": True},
            "GET",
            "/api/v1/user_groups",
            {"include_deactivated_groups": ["true"]},
        ),
        (
            "zulip_get_user_group_members",
            {"user_group_id": 3, "direct_member_only": True},
            "GET",
            "/api/v1/user_groups/3/members",
            {"direct_member_only": ["true"]},
        ),
        (
            "zulip_is_user_group_member",
            {"user_group_id": 3, "user_id": 7},
            "GET",
            "/api/v1/user_groups/3/members/7",
            {"direct_member_only": ["false"]},
        ),
        (
            "zulip_update_status",
            {"status_text": ""},
            "POST",
            "/api/v1/users/me/status",
            {"status_text": [""]},
        ),
        (
            "zulip_update_status",
            {"emoji_name": "wave", "emoji_code": "1f44b"},
            "POST",
            "/api/v1/users/me/status",
            {
                "emoji_name": ["wave"],
                "emoji_code": ["1f44b"],
                "reaction_type": ["unicode_emoji"],
            },
        ),
        (
            "zulip_manage_user_mute",
            {"muted_user_id": 7, "action": "mute"},
            "POST",
            "/api/v1/users/me/muted_users/7",
            {},
        ),
        (
            "zulip_manage_user_mute",
            {"muted_user_id": 7, "action": "unmute"},
            "DELETE",
            "/api/v1/users/me/muted_users/7",
            {},
        ),
    ],
)
async def test_user_operations_preserve_wire_parameters_and_acting_caller(
    tool, arguments, method, path, encoded
):
    from urllib.parse import parse_qs

    calls = []

    def upstream(request):
        calls.append(request)
        assert request.method == method
        assert request.url.path == path
        assert (
            request.headers["authorization"]
            == httpx.BasicAuth("alice", "synthetic")._auth_header
        )
        assert (
            parse_qs(
                request.url.query.decode()
                if method == "GET"
                else request.content.decode(),
                keep_blank_values=True,
            )
            == encoded
        )
        return httpx.Response(200, json={"result": "success", "fixture": "preserved"})

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(
            channel_policy=ChannelPolicy(jd_enabled=True),
            transport=httpx.MockTransport(upstream),
        ),
    )
    result = await server.call_tool(tool, arguments)
    assert not result.is_error
    assert result.structured_content["data"]["fixture"] == "preserved"
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{}, {"status_text": "x" * 61}])
async def test_status_validation_precedes_any_network_request(arguments):
    def upstream(request):
        pytest.fail("Invalid status must not reach Zulip")

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream)),
    )
    with pytest.raises(ToolError):
        await server.call_tool("zulip_update_status", arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool, arguments",
    [
        ("zulip_update_status", {"status_text": "busy"}),
        ("zulip_manage_user_mute", {"muted_user_id": 7}),
    ],
)
async def test_user_writes_are_not_retried_after_server_error(tool, arguments):
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(503, json={"result": "error"})

    server = create_server(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        Executor(transport=httpx.MockTransport(upstream)),
    )
    result = await server.call_tool(tool, arguments)
    assert result.is_error
    assert len(calls) == 1

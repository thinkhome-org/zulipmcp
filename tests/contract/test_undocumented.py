"""Independently enumerated wire inputs from pinned Zulip handlers."""

import pytest

from thinkhome_zulip_mcp.catalog import load_catalog

CASES = [
    ("upload-realm-icon", "POST", "/realm/icon", {"file"}),
    ("delete-realm-icon", "DELETE", "/realm/icon", set()),
    ("get-realm-icon", "GET", "/realm/icon", set()),
    ("upload-realm-logo", "POST", "/realm/logo", {"file", "night"}),
    ("delete-realm-logo", "DELETE", "/realm/logo", {"night"}),
    ("get-realm-logo", "GET", "/realm/logo", {"night"}),
    ("deactivate-realm", "POST", "/realm/deactivate", {"deletion_delay_days"}),
    ("delete-realm-export", "DELETE", "/export/realm/{export_id}", {"export_id"}),
    ("send-submessage", "POST", "/submessage", {"content", "message_id", "msg_type"}),
    ("process-zcommand", "POST", "/zcommand", {"command"}),
    (
        "create-default-stream-group",
        "POST",
        "/default_stream_groups/create",
        {"description", "group_name", "stream_names"},
    ),
    (
        "update-default-stream-group",
        "PATCH",
        "/default_stream_groups/{group_id}",
        {"group_id", "new_description", "new_group_name"},
    ),
    (
        "delete-default-stream-group",
        "DELETE",
        "/default_stream_groups/{group_id}",
        {"group_id"},
    ),
    (
        "update-default-stream-group-streams",
        "PATCH",
        "/default_stream_groups/{group_id}/streams",
        {"group_id", "op", "stream_names"},
    ),
    ("summarize-messages", "GET", "/messages/summary", {"narrow"}),
    ("create-zoom-call", "POST", "/calls/zoom/create", {"is_video_call"}),
    ("get-owned-bots", "GET", "/bots", set()),
    ("deactivate-bot", "DELETE", "/bots/{bot_id}", {"bot_id"}),
    ("upload-own-avatar", "POST", "/users/me/avatar", {"file"}),
    ("delete-own-avatar", "DELETE", "/users/me/avatar", set()),
    (
        "delete-profile-field",
        "DELETE",
        "/realm/profile_fields/{field_id}",
        {"field_id"},
    ),
    (
        "update-profile-field",
        "PATCH",
        "/realm/profile_fields/{field_id}",
        {
            "field_id",
            "display_in_profile_summary",
            "editable_by_user",
            "field_data",
            "hint",
            "name",
            "required",
            "use_for_user_matching",
        },
    ),
    (
        "create-bot",
        "POST",
        "/bots",
        {
            "bot_type",
            "config_data",
            "default_all_public_streams",
            "default_events_register_stream",
            "default_sending_stream",
            "full_name",
            "interface_type",
            "payload_url",
            "service_name",
            "short_name",
            "file",
        },
    ),
    (
        "update-bot",
        "PATCH",
        "/bots/{bot_id}",
        {
            "bot_id",
            "bot_owner_id",
            "config_data",
            "default_all_public_streams",
            "default_events_register_stream",
            "default_sending_stream",
            "full_name",
            "role",
            "service_interface",
            "service_payload_url",
            "short_name",
            "file",
        },
    ),
]


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


@pytest.mark.parametrize("operation_id,method,path,expected", CASES)
def test_hidden_operation_inputs(catalog, operation_id, method, path, expected):
    assert operation_id in catalog, f"Missing source-audited operation {operation_id}"
    op = catalog[operation_id]
    assert (op.method, op.path) == (method, path)
    parameters = {p["name"] for p in op.definition.get("parameters", [])}
    for content in op.definition.get("requestBody", {}).get("content", {}).values():
        parameters |= set(content["schema"]["properties"])
    assert parameters == expected
    assert op.definition["x-thinkhome"]["authentication"] == "zulip_api_key"
    assert op.definition["x-thinkhome"]["evidence"]
    assert op.implementation_status == "unimplemented"


def test_json_string_bot_fields_and_sensitive_results_are_preserved(catalog):
    for name, param in [
        ("create-bot", "payload_url"),
        ("update-bot", "service_payload_url"),
    ]:
        body = catalog[name].definition["requestBody"]["content"]["multipart/form-data"]
        assert body["encoding"][param]["contentType"] == "application/json"
        assert body["schema"]["properties"][param]["type"] == "string"
    assert catalog["create-bot"].definition["x-thinkhome"]["sensitive_response"] is True
    assert (
        catalog["get-owned-bots"].definition["x-thinkhome"]["sensitive_response"]
        is True
    )


def test_get_summary_is_not_safe_to_retry_and_branding_returns_redirects(catalog):
    assert catalog["summarize-messages"].definition["x-thinkhome"]["safe_read"] is False
    for name in ["get-realm-icon", "get-realm-logo"]:
        assert "302" in catalog[name].definition["responses"]


def test_realm_overlay_accounts_for_every_source_parameter(catalog):
    import json
    from importlib.resources import files

    inventory = json.loads(
        files("thinkhome_zulip_mcp").joinpath("data/routes.json").read_text()
    )
    parameters = inventory["handlers"]["zerver.views.realm.update_realm"]["parameters"]
    aliases = {
        "jitsi_server_url_raw": "jitsi_server_url",
        "message_content_delete_limit_seconds_raw": (
            "message_content_delete_limit_seconds"
        ),
        "message_content_edit_limit_seconds_raw": "message_content_edit_limit_seconds",
        "message_retention_days_raw": "message_retention_days",
        "move_messages_between_streams_limit_seconds_raw": (
            "move_messages_between_streams_limit_seconds"
        ),
        "move_messages_within_stream_limit_seconds_raw": (
            "move_messages_within_stream_limit_seconds"
        ),
    }
    expected = {
        aliases.get(p["name"], p["name"])
        for p in parameters
        if p["kind"] == "keyword_only"
    }
    assert "update-realm" in catalog
    content = catalog["update-realm"].definition["requestBody"]["content"][
        "application/x-www-form-urlencoded"
    ]
    assert set(content["schema"]["properties"]) == expected
    assert content["encoding"]["jitsi_server_url"]["contentType"] == "application/json"
    props = content["schema"]["properties"]
    assert props["name"]["maxLength"] == 40
    assert props["digest_weekday"]["enum"] == [0, 1, 2, 3, 4, 5, 6]
    assert props["message_edit_history_visibility_policy"]["enum"] == [
        "all",
        "moves",
        "none",
    ]
    group = props["can_manage_all_groups"]
    assert group["required"] == ["new"]
    assert group["properties"]["old"]["nullable"] is True
    assert (
        "authentication_methods"
        in catalog["update-realm"].definition["x-thinkhome"]["owner_only_parameters"]
    )


def test_bot_operations_support_form_without_an_avatar(catalog):
    for name in ["create-bot", "update-bot"]:
        body = catalog[name].definition["requestBody"]["content"]
        assert "application/x-www-form-urlencoded" in body
        form = body["application/x-www-form-urlencoded"]["schema"]
        multipart = body["multipart/form-data"]["schema"]
        assert "file" not in form["properties"]
        assert "file" in multipart["properties"]
        assert form.get("required", []) == multipart.get("required", [])


def test_subdomain_availability_is_public_and_read_only(catalog):
    assert "check-subdomain-available" in catalog
    op = catalog["check-subdomain-available"]
    assert (op.method, op.path) == ("GET", "/realm/subdomain/{subdomain}")
    assert op.definition["security"] == []
    assert op.definition["parameters"][0]["name"] == "subdomain"
    assert op.definition["x-thinkhome"]["safe_read"] is True


def test_documented_send_operation_keeps_undocumented_parameters(catalog):
    op = catalog["send-message"].definition
    body = op["requestBody"]["content"]["application/x-www-form-urlencoded"]
    for name in ["forged", "sender", "time", "widget_content", "subject"]:
        assert name in body["schema"]["properties"]
    assert body["encoding"]["time"]["contentType"] == "application/json"
    assert body["schema"]["properties"]["time"]["type"] == "number"
    assert "can_forge_sender" in op["x-thinkhome-parameter-audit"]["permission_notes"]


@pytest.mark.parametrize(
    "name",
    [
        "send-message",
        "update-message",
        "create-scheduled-message",
        "update-scheduled-message",
    ],
)
def test_topic_legacy_alias_is_accounted_for(catalog, name):
    body = catalog[name].definition["requestBody"]["content"][
        "application/x-www-form-urlencoded"
    ]
    assert (
        body["schema"]["properties"]["subject"] == body["schema"]["properties"]["topic"]
    )

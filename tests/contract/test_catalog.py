"""Public catalog must preserve source schemas and reject duplicate IDs."""


def test_catalog_preserves_message_operation():
    from thinkhome_zulip_mcp.catalog import load_catalog

    catalog = load_catalog()
    operation = catalog["send-message"]
    assert operation.method == "POST"
    assert operation.path == "/messages"
    assert operation.source_commit == "b31982251ff82d0b02e59f63ad21f19204e37aea"
    body = operation.definition["requestBody"]["content"]
    schema = body["application/x-www-form-urlencoded"]["schema"]
    assert "content" in schema["properties"]
    assert "content" in schema["required"]
    assert operation.implementation_status == "unimplemented"


def test_duplicate_operation_ids_rejected():
    import pytest

    from thinkhome_zulip_mcp.catalog import catalog_from_document

    document = {
        "paths": {
            "/first": {"get": {"operationId": "collision"}},
            "/second": {"post": {"operationId": "collision"}},
        }
    }
    with pytest.raises(ValueError, match="Duplicate operation ID"):
        catalog_from_document(document, source_commit="fixture")


def test_missing_operation_id_rejected():
    import pytest

    from thinkhome_zulip_mcp.catalog import catalog_from_document

    with pytest.raises(ValueError, match="Missing operation ID"):
        catalog_from_document(
            {"paths": {"/test": {"get": {}}}}, source_commit="fixture"
        )


def test_documentation_only_entry_is_not_callable():
    from thinkhome_zulip_mcp.catalog import load_catalog, load_document

    assert "/real-time" in load_document()["paths"]
    assert all(op.path != "/real-time" for op in load_catalog().values())


def test_legacy_settings_aliases_preserve_all_current_parameters():
    from thinkhome_zulip_mcp.catalog import load_catalog

    catalog = load_catalog()
    canonical = next(o for o in catalog.values() if o.path == "/settings")
    for name, path in [
        ("update-settings-display-legacy", "/settings/display"),
        ("update-settings-notifications-legacy", "/settings/notifications"),
    ]:
        alias = catalog[name]
        assert alias.path == path
        assert alias.method == "PATCH"
        assert alias.definition["requestBody"] == canonical.definition["requestBody"]
        assert alias.definition["x-thinkhome"]["authentication"] == "zulip_api_key"
        assert (
            alias.definition["x-thinkhome"]["role_policy"] == "human_users_only; self"
        )


def test_onboarding_overlay_accounts_for_all_handler_inputs():
    from thinkhome_zulip_mcp.catalog import load_catalog

    operation = load_catalog()["mark-onboarding-step-read"]
    assert (operation.method, operation.path) == ("POST", "/users/me/onboarding_steps")
    content = operation.definition["requestBody"]["content"][
        "application/x-www-form-urlencoded"
    ]
    assert set(content["schema"]["properties"]) == {
        "onboarding_step",
        "schedule_navigation_tour_video_reminder_delay",
    }
    assert content["schema"]["required"] == ["onboarding_step"]
    assert content["encoding"]["schedule_navigation_tour_video_reminder_delay"] == {
        "contentType": "application/json",
    }
    assert (
        operation.definition["x-thinkhome"]["role_policy"] == "human_users_only; self"
    )
    assert operation.implementation_status == "unimplemented"


def test_onboarding_optional_delay_is_not_json_nullable():
    from thinkhome_zulip_mcp.catalog import load_catalog

    operation = load_catalog()["mark-onboarding-step-read"]
    schema = operation.definition["requestBody"]["content"][
        "application/x-www-form-urlencoded"
    ]["schema"]
    delay = schema["properties"]["schedule_navigation_tour_video_reminder_delay"]
    assert not delay.get("nullable", False)
    assert "schedule_navigation_tour_video_reminder_delay" not in schema["required"]

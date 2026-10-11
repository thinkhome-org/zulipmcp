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

"""Local OpenAPI references must resolve without fetching external URLs."""

import pytest


def test_reference_chain_and_pointer_escapes():
    from thinkhome_zulip_mcp.catalog import resolve_reference

    document = {
        "components": {"a/b": {"$ref": "#/components/a~0b"}, "a~b": {"type": "string"}}
    }
    assert resolve_reference(document, {"$ref": "#/components/a~1b"}) == {
        "type": "string"
    }


@pytest.mark.parametrize("ref", ["https://example.invalid/schema", "#/missing"])
def test_external_and_missing_references_are_rejected(ref):
    from thinkhome_zulip_mcp.catalog import resolve_reference

    with pytest.raises(ValueError):
        resolve_reference({}, {"$ref": ref})


def test_recursive_reference_chain_fails_without_hanging():
    from thinkhome_zulip_mcp.catalog import resolve_reference

    with pytest.raises(ValueError, match="Cyclic"):
        resolve_reference({"a": {"$ref": "#/a"}}, {"$ref": "#/a"})


def test_reference_must_target_an_object():
    from thinkhome_zulip_mcp.catalog import resolve_reference

    with pytest.raises(ValueError):
        resolve_reference({"a": "string"}, {"$ref": "#/a"})

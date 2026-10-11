"""Audit source syntax without importing or executing the upstream project."""


def test_extracts_methods_handlers_flags_and_conditional_context():
    from thinkhome_zulip_mcp.source_audit import extract_routes

    source = """
from project.views import update as update_view
raise RuntimeError("must never run")
if settings.FEATURE:
    patterns = [rest_path("users/<int:user_id>",
        PATCH=(update_view, {"intentionally_undocumented"}), DELETE=remove)]
"""
    routes = extract_routes(source, "project/urls.py")
    assert len(routes) == 2
    assert routes[0]["pattern"] == "users/<int:user_id>"
    assert routes[0]["method"] == "PATCH"
    assert routes[0]["handler"] == "project.views.update"
    assert routes[0]["flags"] == ["intentionally_undocumented"]
    assert routes[0]["group"] == "patterns"
    assert routes[0]["conditions"] == ["settings.FEATURE"]
    assert routes[1]["method"] == "DELETE"


def test_includes_dynamic_patterns_and_non_rest_methods_remain_unresolved():
    from thinkhome_zulip_mcp.source_audit import extract_routes

    routes = extract_routes(
        """
urls = [path("api/", include(extra_patterns)), path(dynamic_url, view)]
""",
        "urls.py",
    )
    assert routes[0]["kind"] == "include"
    assert routes[0]["handler"] == "include(extra_patterns)"
    assert routes[1]["pattern"] is None
    assert routes[1]["pattern_expression"] == "dynamic_url"
    assert routes[1]["method"] is None
    assert all(r["review_status"] == "pending" for r in routes)


def test_else_branch_context_is_not_confused_with_if():
    from thinkhome_zulip_mcp.source_audit import extract_routes

    routes = extract_routes(
        """
if settings.DEBUG:
    urls = [path("debug", view)]
else:
    urls = [path("production", other_view)]
""",
        "urls.py",
    )
    assert routes[0]["conditions"] == ["settings.DEBUG"]
    assert routes[1]["conditions"] == ["not (settings.DEBUG)"]


def test_build_inventory_records_source_hashes_and_is_deterministic(tmp_path):
    from thinkhome_zulip_mcp.source_audit import build_inventory

    (tmp_path / "zproject").mkdir()
    (tmp_path / "zproject/urls.py").write_text('urls = [rest_path("x", GET=view)]\n')
    first = build_inventory(tmp_path, ["zproject/urls.py"])
    second = build_inventory(tmp_path, ["zproject/urls.py"])
    assert first == second
    assert len(first["source_files"]["zproject/urls.py"]["sha256"]) == 64
    assert first["routes"][0]["pattern"] == "x"
    (tmp_path / "zproject/urls.py").write_text('urls = [rest_path("y", GET=view)]\n')
    changed = build_inventory(tmp_path, ["zproject/urls.py"])
    assert changed["source_files"] != first["source_files"]
    assert changed["routes"][0]["pattern"] == "y"


def test_inventory_preserves_handler_parameters_defaults_and_decorators(tmp_path):
    from thinkhome_zulip_mcp.source_audit import build_inventory

    (tmp_path / "project").mkdir()
    (tmp_path / "project/urls.py").write_text(
        'from project.views import edit\nurls = [rest_path("x", PATCH=edit)]\n'
    )
    (tmp_path / "project/views.py").write_text("""
@human_users_only
@typed_endpoint
def edit(request, user, *, name: str, count: Json[int] = 4):
    raise RuntimeError("not executed")
""")
    inventory = build_inventory(tmp_path, ["project/urls.py"])
    handler = inventory["handlers"]["project.views.edit"]
    assert handler["decorators"] == ["human_users_only", "typed_endpoint"]
    assert handler["parameters"] == [
        {"name": "request", "kind": "positional", "annotation": None, "default": None},
        {"name": "user", "kind": "positional", "annotation": None, "default": None},
        {"name": "name", "kind": "keyword_only", "annotation": "str", "default": None},
        {
            "name": "count",
            "kind": "keyword_only",
            "annotation": "Json[int]",
            "default": "4",
        },
    ]
    assert handler["source_file"] == "project/views.py"
    assert "project/views.py" in inventory["source_files"]

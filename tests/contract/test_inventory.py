"""Source audit is reproducible and never counts unreviewed routes as coverage."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_catalog_check_runs_without_upstream_checkout():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/catalog.py"), "check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["source_commit"] == "b31982251ff82d0b02e59f63ad21f19204e37aea"
    assert report["pending_routes"] > 0
    assert report["full_coverage"] is False


def test_release_coverage_gate_rejects_pending_audit():
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/catalog.py"),
            "check",
            "--require-reviewed",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "audit incomplete" in result.stderr.lower()


def test_every_core_rest_route_has_catalog_metadata():
    import re

    from thinkhome_zulip_mcp.catalog import load_catalog

    data = ROOT / "src/thinkhome_zulip_mcp/data"
    inventory = json.loads((data / "routes.json").read_text())
    operations = {(op.method, op.path) for op in load_catalog().values()}
    missing = []
    for route in inventory["routes"]:
        if (route["source_file"], route["group"]) != (
            "zproject/urls.py",
            "v1_api_and_json_patterns",
        ) or route["method"] is None:
            continue
        path = "/" + re.sub(r"<(?:\w+:)?([^>]+)>", r"{\1}", route["pattern"])
        if (route["method"], path) not in operations:
            missing.append((route["method"], path))
    assert not missing, missing


def test_every_core_typed_handler_parameter_is_accounted_for():
    import re

    from thinkhome_zulip_mcp.catalog import (
        load_catalog,
        load_document,
        resolve_reference,
    )

    data = ROOT / "src/thinkhome_zulip_mcp/data"
    inventory = json.loads((data / "routes.json").read_text())
    operations = {(op.method, op.path): op for op in load_catalog().values()}
    document = load_document()
    missing = []
    for route in inventory["routes"]:
        if (route["source_file"], route["group"]) != (
            "zproject/urls.py",
            "v1_api_and_json_patterns",
        ) or route["method"] is None:
            continue
        path = "/" + re.sub(r"<(?:\w+:)?([^>]+)>", r"{\1}", route["pattern"])
        operation = operations[(route["method"], path)]
        names = {
            resolve_reference(document, p)["name"]
            for p in (
                operation.path_parameters + operation.definition.get("parameters", [])
            )
        }
        body = resolve_reference(document, operation.definition.get("requestBody", {}))
        for content in body.get("content", {}).values():
            names.update(
                resolve_reference(document, content["schema"]).get("properties", {})
            )
        for parameter in inventory["handlers"][route["handler"]]["parameters"]:
            if parameter["kind"] != "keyword_only":
                continue
            annotation = parameter["annotation"] or ""
            name = parameter["name"]
            alias = re.search(r"ApiParamConfig\('([^']+)'", annotation)
            if alias and "path_only=True" not in annotation:
                name = alias.group(1)
            if annotation == "OptionalTopic":
                name = "topic"
                assert "subject" in names
            if name not in names:
                missing.append((operation.operation_id, name))
    assert not missing, missing

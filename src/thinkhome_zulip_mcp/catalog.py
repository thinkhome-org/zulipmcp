"""Load the pinned upstream schema without discarding parameter definitions."""

import json
from copy import deepcopy
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

import yaml

HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})


@dataclass(frozen=True)
class Operation:
    operation_id: str
    method: str
    path: str
    source_commit: str
    definition: dict[str, Any]
    path_parameters: list[dict[str, Any]]
    implementation_status: str = "unimplemented"


def catalog_from_document(
    document: dict[str, Any], *, source_commit: str
) -> dict[str, Operation]:
    catalog: dict[str, Operation] = {}
    for path, path_item in document["paths"].items():
        for method, definition in path_item.items():
            if method not in HTTP_METHODS:
                continue
            operation_id = definition.get("operationId")
            if not isinstance(operation_id, str) or not operation_id:
                raise ValueError(f"Missing operation ID: {method} {path}")
            if operation_id in catalog:
                raise ValueError(f"Duplicate operation ID: {operation_id}")
            catalog[operation_id] = Operation(
                operation_id=operation_id,
                method=method.upper(),
                path=path,
                source_commit=source_commit,
                definition=definition,
                path_parameters=path_item.get("parameters", []),
            )
    return dict(sorted(catalog.items()))


def load_document() -> dict[str, Any]:
    document = yaml.safe_load(
        files("thinkhome_zulip_mcp").joinpath("data/zulip.yaml").read_text()
    )
    if not isinstance(document, dict):
        raise ValueError("Invalid bundled OpenAPI document")
    overlays = json.loads(
        files("thinkhome_zulip_mcp").joinpath("data/overlays.json").read_text()
    )
    for alias in overlays["aliases"]:
        definition = deepcopy(
            document["paths"][alias["target_path"]][alias["target_method"]]
        )
        definition["operationId"] = alias["operationId"]
        definition["x-thinkhome"] = alias["metadata"]
        destination = document["paths"].setdefault(alias["path"], {})
        if alias["method"] in destination:
            raise ValueError(
                f"Overlay overwrites an upstream operation: {alias['path']}"
            )
        destination[alias["method"]] = definition
    for path, item in overlays["paths"].items():
        destination = document["paths"].setdefault(path, {})
        for method, definition in item.items():
            if method in destination:
                raise ValueError(f"Overlay overwrites an upstream operation: {path}")
            destination[method] = definition
    return document


def load_catalog() -> dict[str, Operation]:
    provenance = json.loads(
        files("thinkhome_zulip_mcp").joinpath("data/provenance.json").read_text()
    )
    document = load_document()
    exclusions = json.loads(
        files("thinkhome_zulip_mcp").joinpath("data/exclusions.json").read_text()
    )
    for path in exclusions:
        if path not in document["paths"]:
            raise ValueError(f"Stale catalog exclusion: {path}")
        del document["paths"][path]
    return catalog_from_document(document, source_commit=provenance["stable_commit"])

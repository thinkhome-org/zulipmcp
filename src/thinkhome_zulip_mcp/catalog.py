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
    for addition in overlays.get("parameter_additions", []):
        definition = document["paths"][addition["path"]][addition["method"]]
        body = deepcopy(resolve_reference(document, definition["requestBody"]))
        content = body["content"][addition["media_type"]]
        schema = deepcopy(resolve_reference(document, content["schema"]))
        target = schema
        for segment in addition.get("schema_path", []):
            target = target[segment]
        properties = target["properties"]
        for name, value in addition["properties"].items():
            if name in properties:
                raise ValueError(f"Parameter overlay overwrites upstream input: {name}")
            properties[name] = value
        for alias, canonical in addition["aliases"].items():
            if alias in properties:
                raise ValueError(f"Parameter alias overwrites upstream input: {alias}")
            properties[alias] = deepcopy(properties[canonical])
            schema.setdefault("allOf", []).append(
                {"not": {"required": [alias, canonical]}}
            )
        content["schema"] = schema
        content.setdefault("encoding", {}).update(addition["encoding"])
        definition["requestBody"] = body
        definition["x-thinkhome-parameter-audit"] = addition["metadata"]
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


def resolve_reference(
    document: dict[str, Any], value: dict[str, Any]
) -> dict[str, Any]:
    """Resolve an OpenAPI Reference Object using only this pinned document."""
    seen = set()
    while "$ref" in value:
        ref = value["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise ValueError("Only local OpenAPI references are allowed")
        if ref in seen:
            raise ValueError(f"Cyclic OpenAPI reference: {ref}")
        seen.add(ref)
        target: Any = document
        try:
            for part in ref[2:].split("/"):
                key = part.replace("~1", "/").replace("~0", "~")
                target = target[key]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"Missing OpenAPI reference: {ref}") from exc
        if not isinstance(target, dict):
            raise ValueError(f"OpenAPI reference is not an object: {ref}")
        value = target
    return value

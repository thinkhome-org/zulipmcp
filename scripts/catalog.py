"""Reproduce pinned route evidence; classification and execution are separate gates."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from thinkhome_zulip_mcp.catalog import load_catalog
from thinkhome_zulip_mcp.source_audit import build_inventory

DATA = Path(__file__).resolve().parents[1] / "src/thinkhome_zulip_mcp/data"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "update"])
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--require-reviewed", action="store_true")
    args = parser.parse_args()
    provenance = json.loads((DATA / "provenance.json").read_text())
    if (
        hashlib.sha256((DATA / "zulip.yaml").read_bytes()).hexdigest()
        != provenance["schema_sha256"]
    ):
        raise ValueError("Bundled schema differs from pinned SHA-256")
    inventory_path = DATA / "routes.json"
    inventory = json.loads(inventory_path.read_text())
    if inventory["source_commit"] != provenance["stable_commit"]:
        raise ValueError("Route inventory belongs to a different upstream commit")
    if args.command == "update" and args.source_root is None:
        parser.error(
            "update requires --source-root pointing to the pinned Zulip source"
        )
    if args.source_root:
        source_schema = args.source_root / provenance["source_path"]
        if (
            hashlib.sha256(source_schema.read_bytes()).hexdigest()
            != provenance["schema_sha256"]
        ):
            raise ValueError("Upstream source schema does not match the pinned release")
        rebuilt = build_inventory(args.source_root, inventory["route_source_paths"])
        rebuilt["source_commit"] = provenance["stable_commit"]
        rebuilt["route_source_paths"] = inventory["route_source_paths"]
        if args.command == "update":
            inventory_path.write_text(
                json.dumps(rebuilt, indent=2, sort_keys=True) + "\n"
            )
            inventory = rebuilt
        elif inventory != rebuilt:
            raise ValueError(
                "Route evidence differs from upstream source; review and update"
            )
    catalog = load_catalog()
    pending = sum(route["review_status"] != "reviewed" for route in inventory["routes"])
    unimplemented = sum(
        op.implementation_status != "implemented" for op in catalog.values()
    )
    report = {
        "source_commit": provenance["stable_commit"],
        "schema_operations": len(catalog),
        "route_records": len(inventory["routes"]),
        "handler_records": len(inventory["handlers"]),
        "pending_routes": pending,
        "unimplemented_schema_operations": unimplemented,
        "full_coverage": pending == 0 and unimplemented == 0,
        "source_reproduced": args.source_root is not None,
    }
    print(json.dumps(report, indent=2))
    if args.require_reviewed and (pending or unimplemented):
        print(
            "API audit incomplete: pending routes or unimplemented operations",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

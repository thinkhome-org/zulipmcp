"""Report Zulip upstream changes without updating any pin or bundled source."""

import hashlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path

import httpx


def compare(
    pin: Mapping[str, str],
    latest_tag: str,
    stable_commit: str,
    main_commit: str,
    schema: bytes,
) -> list[str]:
    return [
        description
        for changed, description in (
            (latest_tag != pin["stable_tag"], "new stable release"),
            (stable_commit != pin["stable_commit"], "pinned stable tag moved"),
            (main_commit != pin["main_commit"], "upstream main changed"),
            (
                hashlib.sha256(schema).hexdigest() != pin["schema_sha256"],
                "pinned schema differs from upstream",
            ),
        )
        if changed
    ]


def main() -> int:
    data = Path(__file__).resolve().parents[1] / "src/thinkhome_zulip_mcp/data"
    pin = json.loads((data / "provenance.json").read_text())
    with httpx.Client(timeout=30, follow_redirects=False) as client:

        def field(path: str, key: str) -> str:
            response = client.get("https://api.github.com/repos/zulip/zulip/" + path)
            response.raise_for_status()
            value = response.json()[key]
            if not isinstance(value, str) or not value:
                raise ValueError("Malformed GitHub upstream response")
            return value

        latest = field("releases/latest", "tag_name")
        stable = field("commits/" + pin["stable_tag"], "sha")
        upstream_main = field("commits/main", "sha")
        response = client.get(
            "https://raw.githubusercontent.com/zulip/zulip/"
            + pin["stable_commit"]
            + "/"
            + pin["source_path"]
        )
        response.raise_for_status()
    changes = compare(pin, latest, stable, upstream_main, response.content)
    print(
        json.dumps(
            {
                "latest_release": latest,
                "main_commit": upstream_main,
                "changes": changes,
            },
            indent=2,
        )
    )
    return int(bool(changes))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Upstream verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

import hashlib
import runpy
from pathlib import Path

compare = runpy.run_path(
    str(Path(__file__).resolve().parents[2] / "scripts/upstream_drift.py")
)["compare"]


def test_unchanged_release_main_and_schema_are_clean():
    pin = {
        "stable_tag": "12.3",
        "stable_commit": "stable",
        "main_commit": "main",
        "schema_sha256": hashlib.sha256(b"schema").hexdigest(),
    }
    assert compare(pin, "12.3", "stable", "main", b"schema") == []


def test_each_independent_change_is_reported_without_rewriting_the_pin():
    pin = {
        "stable_tag": "12.3",
        "stable_commit": "stable",
        "main_commit": "main",
        "schema_sha256": hashlib.sha256(b"schema").hexdigest(),
    }
    original = dict(pin)
    assert compare(pin, "12.4", "moved-tag", "new-main", b"changed") == [
        "new stable release",
        "pinned stable tag moved",
        "upstream main changed",
        "pinned schema differs from upstream",
    ]
    assert pin == original

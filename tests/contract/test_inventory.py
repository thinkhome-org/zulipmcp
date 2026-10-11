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

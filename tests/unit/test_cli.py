"""Catch broken CLI packaging and unexpected import-time network access."""

import subprocess
import sys


def test_cli_version():
    result = subprocess.run(
        [sys.executable, "-m", "thinkhome_zulip_mcp", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("thinkhome-zulip-mcp ")
    assert not result.stderr


def test_import_has_no_network_access():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
def deny_network(event, args):
    if event in ('socket.connect', 'socket.getaddrinfo', 'socket.bind'):
        raise RuntimeError('Network access during import')
sys.addaudithook(deny_network)
import thinkhome_zulip_mcp
import thinkhome_zulip_mcp.cli
""",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stdout
    assert not result.stderr

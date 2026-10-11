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


def test_http_cli_uses_provisioned_auth_without_stdio_credentials(
    tmp_path, monkeypatch
):
    import uvicorn
    from cryptography.fernet import Fernet

    from thinkhome_zulip_mcp.cli import main

    key_file = tmp_path / "key"
    key_file.write_bytes(Fernet.generate_key())
    key_file.chmod(0o600)
    monkeypatch.setenv("MCP_STORE_PATH", str(tmp_path / "db"))
    monkeypatch.setenv("MCP_KEY_FILE", str(key_file))
    monkeypatch.setenv("MCP_RESOURCE_URL", "https://mcp.example/mcp")
    monkeypatch.setenv("ZULIP_URL", "https://zulip.example")
    monkeypatch.delenv("ZULIP_API_KEY", raising=False)
    monkeypatch.delenv("ZULIP_API_KEY_FILE", raising=False)
    monkeypatch.setattr(
        sys, "argv", ["thinkhome-zulip-mcp", "serve", "--transport", "http"]
    )
    calls = []
    monkeypatch.setattr(
        uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs))
    )
    main()
    assert len(calls) == 1
    assert calls[0][1]["host"] == "127.0.0.1"
    assert calls[0][1]["access_log"] is False
    assert calls[0][1]["proxy_headers"] is False

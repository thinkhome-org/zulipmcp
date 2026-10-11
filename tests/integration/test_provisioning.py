"""Provisioning secrets are files, never shell arguments or command output."""

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from cryptography.fernet import Fernet

from thinkhome_zulip_mcp.store import IdentityStore


def test_provisioning_lifecycle(tmp_path: Path) -> None:
    key_file = tmp_path / "key"
    db_file = tmp_path / "identities.db"
    api_file = tmp_path / "zulip-key"
    token_file = tmp_path / "token"
    api_file.write_text("synthetic-zulip-secret")
    api_file.chmod(0o600)
    env = {**os.environ, "MCP_STORE_PATH": str(db_file), "MCP_KEY_FILE": str(key_file)}

    def run(*args: str, success: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, "-m", "thinkhome_zulip_mcp", *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert (result.returncode == 0) is success, result.stderr
        assert "synthetic-zulip-secret" not in result.stdout + result.stderr
        return result

    run("secrets", "init")
    key = key_file.read_bytes().strip()
    assert key_file.stat().st_mode & 0o777 == 0o600
    run("secrets", "init", success=False)
    assert key_file.read_bytes().strip() == key
    identity_args = (
        "--id",
        "alice",
        "--realm",
        "https://zulip.example",
        "--email",
        "alice@example",
        "--api-key-file",
        str(api_file),
    )
    run("identity", "add", *identity_args)
    run(
        "token",
        "issue",
        "--id",
        "alice",
        "--expires-at",
        str(int(time.time()) + 300),
        "--output",
        str(token_file),
    )
    with sqlite3.connect(db_file) as db:
        count_before = db.execute("SELECT count(*) FROM tokens").fetchone()[0]
    run(
        "token",
        "issue",
        "--id",
        "alice",
        "--expires-at",
        str(int(time.time()) + 300),
        "--output",
        str(token_file),
        success=False,
    )
    with sqlite3.connect(db_file) as db:
        assert db.execute("SELECT count(*) FROM tokens").fetchone()[0] == count_before
    token = token_file.read_text().strip()
    assert token_file.stat().st_mode & 0o777 == 0o600
    assert IdentityStore(db_file, key).authenticate(token).identity_id == "alice"
    api_file.write_text("changed-synthetic-key")
    run("identity", "update", *identity_args)
    assert (
        IdentityStore(db_file, key).authenticate(token).api_key
        == "changed-synthetic-key"
    )
    new_key = tmp_path / "new-key"
    new_key.write_bytes(Fernet.generate_key())
    new_key.chmod(0o600)
    run("secrets", "rotate", "--new-key-file", str(new_key))
    env["MCP_KEY_FILE"] = str(new_key)
    run("token", "revoke", "--token-file", str(token_file))
    run("identity", "remove", "--id", "alice")
    run(
        "token",
        "issue",
        "--id",
        "alice",
        "--expires-at",
        str(int(time.time()) + 300),
        "--output",
        str(tmp_path / "missing-token"),
        success=False,
    )

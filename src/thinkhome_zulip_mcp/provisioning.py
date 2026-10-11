"""Local provisioning commands; run as the MCP service's operating-system owner."""

from __future__ import annotations

import argparse
import getpass
import os
import stat
import sys
from pathlib import Path

from cryptography.fernet import Fernet

from thinkhome_zulip_mcp.executor import Principal
from thinkhome_zulip_mcp.store import IdentityStore


def add_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    identity = commands.add_parser("identity", help="Provision Zulip identities")
    actions = identity.add_subparsers(dest="action", required=True)
    for name in ("add", "update"):
        action = actions.add_parser(name)
        action.add_argument("--id", required=True)
        action.add_argument("--realm", required=True)
        action.add_argument("--email", required=True)
        action.add_argument("--api-key-file", type=Path)
        action.add_argument("--allow-insecure-http", action="store_true")
    actions.add_parser("remove").add_argument("--id", required=True)
    bind = actions.add_parser("bind-oauth")
    bind.add_argument("--id", required=True)
    bind.add_argument("--issuer", required=True)
    bind.add_argument("--subject", required=True)

    token = commands.add_parser("token", help="Issue or revoke manual MCP tokens")
    actions = token.add_subparsers(dest="action", required=True)
    issue = actions.add_parser("issue")
    issue.add_argument("--id", required=True)
    issue.add_argument("--expires-at", type=int, required=True)
    issue.add_argument("--output", type=Path, required=True)
    actions.add_parser("revoke").add_argument("--token-file", type=Path, required=True)

    secrets = commands.add_parser("secrets", help="Manage the external encryption key")
    actions = secrets.add_subparsers(dest="action", required=True)
    actions.add_parser("init")
    actions.add_parser("rotate").add_argument(
        "--new-key-file", type=Path, required=True
    )


def read_secret(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("Secret must be a regular file with private permissions")
        try:
            value = source.read(65537)
        except UnicodeError:
            raise ValueError("Secret file must contain UTF-8 text") from None
    if len(value) > 65536 or not value.strip():
        raise ValueError("Secret file is empty or too large")
    return value.strip()


def write_secret(path: Path, value: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as target:
        target.write(value + "\n")
        target.flush()
        os.fsync(target.fileno())


def run(args: argparse.Namespace) -> None:
    key_path = os.environ.get("MCP_KEY_FILE")
    if not key_path:
        raise ValueError("MCP_KEY_FILE is required")
    if args.command == "secrets" and args.action == "init":
        write_secret(Path(key_path), Fernet.generate_key().decode())
        print("Created encryption key file.")
        return
    db_path = os.environ.get("MCP_STORE_PATH")
    if not db_path:
        raise ValueError("MCP_STORE_PATH is required")
    store = IdentityStore(Path(db_path), read_secret(Path(key_path)).encode())
    if args.command == "identity":
        if args.action in ("add", "update"):
            if not args.api_key_file and not sys.stdin.isatty():
                raise ValueError("Use --api-key-file when no terminal is available")
            key = (
                read_secret(args.api_key_file)
                if args.api_key_file
                else getpass.getpass("Zulip API key: ")
            )
            principal = Principal(
                args.id, args.realm, args.email, key, args.allow_insecure_http
            )
            if args.action == "add":
                store.add_identity(principal)
            else:
                store.update_identity(principal)
        elif args.action == "remove":
            store.remove_identity(args.id)
        else:
            store.bind_oauth(args.id, issuer=args.issuer, subject=args.subject)
    elif args.command == "token":
        if args.action == "issue":
            token = store.issue_token(args.id, expires_at=args.expires_at)
            try:
                write_secret(args.output, token)
            except OSError:
                store.revoke_token(token)
                raise
        else:
            store.revoke_token(read_secret(args.token_file))
    else:
        store.rotate_key(read_secret(args.new_key_file).encode())
        print("Rotated database key. Set MCP_KEY_FILE to the new key before restart.")
        return
    print("Provisioning operation completed.")

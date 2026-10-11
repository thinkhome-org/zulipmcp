"""Local administrator-managed identities; never accepts model-supplied credentials."""

import hashlib
import json
import os
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from thinkhome_zulip_mcp.executor import Principal


class AuthenticationError(ValueError):
    """An absent, expired or revoked credential; no identity details exposed."""


class IdentityStore:
    def __init__(self, path: Path, key: bytes) -> None:
        self.path = path
        self._cipher = Fernet(key)
        if path.is_symlink():
            raise ValueError("Identity database must not be a symlink")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if not path.is_file() or path.stat().st_mode & 0o077:
                raise ValueError(
                    "Identity database requires private permissions"
                ) from None
        else:
            os.close(fd)
        with self._connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("Unsupported identity database schema")
            if version == 0:
                if db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchone():
                    raise ValueError("Unrecognized identity database schema")
                db.execute("CREATE TABLE metadata (key_check BLOB NOT NULL)")
                db.execute(
                    "INSERT INTO metadata VALUES (?)",
                    (self._cipher.encrypt(b"thinkhome-store-v1"),),
                )
                db.execute(
                    "CREATE TABLE identities (id TEXT PRIMARY KEY, secret BLOB NOT "
                    "NULL)"
                )
                db.execute(
                    "CREATE TABLE tokens (digest TEXT PRIMARY KEY, identity_id TEXT "
                    "NOT NULL REFERENCES identities(id) ON DELETE CASCADE, expires_at "
                    "INTEGER NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE oauth (issuer TEXT NOT NULL, subject TEXT NOT NULL, "
                    "identity_id TEXT NOT NULL REFERENCES identities(id) ON DELETE "
                    "CASCADE, PRIMARY KEY (issuer, subject))"
                )
                db.execute("PRAGMA user_version = 1")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("BEGIN IMMEDIATE")
            with db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1):
                    raise ValueError("Unsupported identity database schema")
                if version == 1:
                    try:
                        marker = self._cipher.decrypt(
                            db.execute("SELECT key_check FROM metadata").fetchone()[0]
                        )
                        if marker != b"thinkhome-store-v1":
                            raise InvalidToken
                    except InvalidToken:
                        raise ValueError("Incorrect identity encryption key") from None
                yield db
        finally:
            db.close()

    def _encode(self, principal: Principal) -> bytes:
        return self._cipher.encrypt(json.dumps(asdict(principal)).encode())

    def _decode(self, identity_id: str, secret: bytes) -> Principal:
        try:
            principal = Principal(**json.loads(self._cipher.decrypt(secret)))
            if principal.identity_id != identity_id:
                raise ValueError("Identity mismatch")
            return principal
        except (InvalidToken, ValueError, TypeError):
            raise AuthenticationError("Invalid stored identity") from None

    def add_identity(self, principal: Principal) -> None:
        try:
            with self._connect() as db:
                db.execute(
                    "INSERT INTO identities VALUES (?, ?)",
                    (principal.identity_id, self._encode(principal)),
                )
        except sqlite3.IntegrityError:
            raise ValueError("Identity already exists") from None

    def update_identity(self, principal: Principal) -> None:
        with self._connect() as db:
            result = db.execute(
                "UPDATE identities SET secret = ? WHERE id = ?",
                (self._encode(principal), principal.identity_id),
            )
            if result.rowcount != 1:
                raise ValueError("Unknown identity")

    def remove_identity(self, identity_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM identities WHERE id = ?", (identity_id,))

    def issue_token(self, identity_id: str, *, expires_at: int) -> str:
        if type(expires_at) is not int or expires_at <= time.time():
            raise ValueError("Token expiration must be a future Unix timestamp")
        token = "thm_" + secrets.token_urlsafe(32)
        with self._connect() as db:
            if not db.execute(
                "SELECT 1 FROM identities WHERE id = ?", (identity_id,)
            ).fetchone():
                raise ValueError("Unknown identity")
            db.execute(
                "INSERT INTO tokens VALUES (?, ?, ?)",
                (self._digest(token), identity_id, expires_at),
            )
        return token

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def revoke_token(self, token: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM tokens WHERE digest = ?", (self._digest(token),))

    def authenticate(self, bearer: str) -> Principal:
        if not bearer.startswith("thm_") or len(bearer) > 128:
            raise AuthenticationError("Invalid credential")
        with self._connect() as db:
            row = db.execute(
                "SELECT i.id, i.secret FROM tokens t JOIN identities i ON "
                "i.id=t.identity_id WHERE t.digest=? AND t.expires_at>?",
                (self._digest(bearer), time.time()),
            ).fetchone()
            if row is None:
                raise AuthenticationError("Invalid credential")
            return self._decode(*row)

    def bind_oauth(self, identity_id: str, *, issuer: str, subject: str) -> None:
        if not issuer or not subject:
            raise ValueError("Issuer and subject must be nonempty")
        try:
            with self._connect() as db:
                db.execute(
                    "INSERT INTO oauth VALUES (?, ?, ?)", (issuer, subject, identity_id)
                )
        except sqlite3.IntegrityError:
            raise ValueError("Unknown identity or existing OAuth binding") from None

    def oauth_principal(self, issuer: str, subject: str) -> Principal:
        # Call only after signature, issuer, audience, expiry and scope validation.
        with self._connect() as db:
            row = db.execute(
                "SELECT i.id, i.secret FROM oauth o JOIN identities i ON "
                "i.id=o.identity_id WHERE o.issuer=? AND o.subject=?",
                (issuer, subject),
            ).fetchone()
            if row is None:
                raise AuthenticationError("Invalid credential")
            return self._decode(*row)

    def rotate_key(self, new_key: bytes) -> None:
        replacement = Fernet(new_key)
        with self._connect() as db:
            for identity_id, secret in db.execute(
                "SELECT id, secret FROM identities"
            ).fetchall():
                principal = self._decode(identity_id, secret)
                db.execute(
                    "UPDATE identities SET secret=? WHERE id=?",
                    (
                        replacement.encrypt(json.dumps(asdict(principal)).encode()),
                        identity_id,
                    ),
                )
            db.execute(
                "UPDATE metadata SET key_check=?",
                (replacement.encrypt(b"thinkhome-store-v1"),),
            )
        self._cipher = replacement

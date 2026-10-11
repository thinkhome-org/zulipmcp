import sqlite3
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from thinkhome_zulip_mcp.executor import Principal
from thinkhome_zulip_mcp.store import AuthenticationError, IdentityStore


def principal(name: str = "alice") -> Principal:
    return Principal(
        name, "https://zulip.example", f"{name}@example.com", f"secret-{name}"
    )


def test_encrypted_persistence_and_hashed_tokens(tmp_path: Path) -> None:
    path = tmp_path / "identities.db"
    key = Fernet.generate_key()
    store = IdentityStore(path, key)
    store.add_identity(principal())
    token = store.issue_token("alice", expires_at=int(time.time()) + 600)
    assert IdentityStore(path, key).authenticate(token) == principal()
    data = path.read_bytes()
    assert b"secret-alice" not in data
    assert token.encode() not in data
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="key"):
        IdentityStore(path, Fernet.generate_key())


def test_revoked_token_is_rejected(tmp_path: Path) -> None:
    store = IdentityStore(tmp_path / "db", Fernet.generate_key())
    store.add_identity(principal())
    token = store.issue_token("alice", expires_at=int(time.time()) + 60)
    store.revoke_token(token)
    with pytest.raises(AuthenticationError):
        store.authenticate(token)
    with pytest.raises(AuthenticationError):
        store.authenticate("unknown-token")


def test_expiry_removal_and_update_apply_immediately(tmp_path: Path) -> None:
    store = IdentityStore(tmp_path / "db", Fernet.generate_key())
    store.add_identity(principal())
    now = int(time.time())
    with pytest.raises(ValueError):
        store.issue_token("alice", expires_at=now - 1)
    token = store.issue_token("alice", expires_at=now + 100)
    store.update_identity(
        Principal("alice", "https://zulip.example", "new@example", "new-key")
    )
    assert store.authenticate(token).api_key == "new-key"
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE tokens SET expires_at = ?", (now - 1,))
    with pytest.raises(AuthenticationError):
        store.authenticate(token)
    token = store.issue_token("alice", expires_at=now + 100)
    store.remove_identity("alice")
    with pytest.raises(AuthenticationError):
        store.authenticate(token)


def test_two_users_never_share_credentials(tmp_path: Path) -> None:
    store = IdentityStore(tmp_path / "db", Fernet.generate_key())
    tokens = {}
    for name in ("alice", "bob"):
        store.add_identity(principal(name))
        tokens[name] = store.issue_token(name, expires_at=int(time.time()) + 100)
    for name, token in tokens.items():
        assert store.authenticate(token) == principal(name)
    store.bind_oauth("alice", issuer="https://issuer.example", subject="123")
    assert store.oauth_principal("https://issuer.example", "123") == principal()
    with pytest.raises(AuthenticationError):
        store.oauth_principal("https://other.example", "123")
    with pytest.raises(AuthenticationError):
        store.oauth_principal("https://issuer.example", "unknown")
    with pytest.raises(ValueError):
        store.bind_oauth("bob", issuer="https://issuer.example", subject="123")


def test_secret_rotation_preserves_access(tmp_path: Path) -> None:
    old_key, new_key = Fernet.generate_key(), Fernet.generate_key()
    store = IdentityStore(tmp_path / "db", old_key)
    store.add_identity(principal())
    token = store.issue_token("alice", expires_at=int(time.time()) + 100)
    store.rotate_key(new_key)
    assert store.authenticate(token) == principal()
    assert IdentityStore(store.path, new_key).authenticate(token) == principal()
    with pytest.raises(ValueError):
        IdentityStore(store.path, old_key)


def test_duplicate_and_missing_identity_fail_closed(tmp_path: Path) -> None:
    store = IdentityStore(tmp_path / "db", Fernet.generate_key())
    store.add_identity(principal())
    with pytest.raises(ValueError):
        store.add_identity(principal())
    with pytest.raises(ValueError):
        store.update_identity(principal("missing"))
    with pytest.raises(ValueError):
        store.issue_token("missing", expires_at=int(time.time()) + 100)


def test_newer_schema_and_insecure_files_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "db"
    key = Fernet.generate_key()
    IdentityStore(path, key)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 999")
    with pytest.raises(ValueError, match="schema"):
        IdentityStore(path, key)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="permissions"):
        IdentityStore(path, key)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="symlink"):
        IdentityStore(link, key)


def test_rotation_rejects_stale_writers_and_rolls_back_corruption(
    tmp_path: Path,
) -> None:
    key = Fernet.generate_key()
    store = IdentityStore(tmp_path / "db", key)
    stale = IdentityStore(store.path, key)
    store.add_identity(principal())
    store.rotate_key(Fernet.generate_key())
    with pytest.raises(ValueError, match="key"):
        stale.add_identity(principal("bob"))
    with sqlite3.connect(store.path) as db:
        original_marker = db.execute("SELECT key_check FROM metadata").fetchone()[0]
        db.execute("UPDATE identities SET secret = ?", (b"corrupt",))
    with pytest.raises(AuthenticationError):
        store.rotate_key(Fernet.generate_key())
    with sqlite3.connect(store.path) as db:
        assert (
            db.execute("SELECT key_check FROM metadata").fetchone()[0]
            == original_marker
        )


def test_swapped_encrypted_identity_is_rejected(tmp_path: Path) -> None:
    store = IdentityStore(tmp_path / "db", Fernet.generate_key())
    store.add_identity(principal())
    store.add_identity(principal("bob"))
    token = store.issue_token("alice", expires_at=int(time.time()) + 100)
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE identities SET secret = "
            "(SELECT secret FROM identities WHERE id='bob') WHERE id='alice'"
        )
    with pytest.raises(AuthenticationError):
        store.authenticate(token)


def test_concurrent_authentication_keeps_principals_separate(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    key = Fernet.generate_key()
    store = IdentityStore(tmp_path / "db", key)
    pairs = []
    for name in ("alice", "bob"):
        store.add_identity(principal(name))
        pairs.append((name, store.issue_token(name, expires_at=int(time.time()) + 100)))

    def authenticate(pair: tuple[str, str]) -> None:
        name, token = pair
        assert IdentityStore(store.path, key).authenticate(token) == principal(name)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(authenticate, pairs * 10))

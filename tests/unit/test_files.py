from pathlib import Path

import pytest

from thinkhome_zulip_mcp.executor import Principal
from thinkhome_zulip_mcp.files import FileAccessError, FileStore

ALICE = Principal("alice", "https://zulip.example", "alice", "test-key")
BOB = Principal("bob", "https://zulip.example", "bob", "test-key")


async def chunks(*values):
    for value in values:
        yield value


@pytest.mark.asyncio
async def test_file_handle_cannot_cross_users(tmp_path: Path):
    with FileStore(parent=tmp_path) as files:
        info = await files.stage(
            ALICE, "a.bin", "application/octet-stream", chunks(b"\x00\xff")
        )
        with files.open(ALICE, info.handle) as (metadata, stream):
            assert metadata == info
            assert stream.read() == b"\x00\xff"
        with pytest.raises(FileAccessError):
            with files.open(BOB, info.handle):
                pass
        with pytest.raises(FileAccessError):
            with files.open(ALICE, "../../outside"):
                pass
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_size_limit_is_enforced_while_streaming(tmp_path: Path):
    consumed = []

    async def too_large():
        for value in (b"abc", b"def", b"never"):
            consumed.append(value)
            yield value

    with FileStore(parent=tmp_path, max_bytes=5) as files:
        with pytest.raises(FileAccessError, match="size"):
            await files.stage(ALICE, "a", "text/plain", too_large())
        assert consumed == [b"abc", b"def"]
        assert list(files.directory.iterdir()) == []


@pytest.mark.asyncio
async def test_expiry_and_shutdown_remove_files(tmp_path: Path):
    now = [1000.0]
    with FileStore(parent=tmp_path, clock=lambda: now[0]) as files:
        info = await files.stage(ALICE, "a", "text/plain", chunks(b"hello"))
        assert info.expires_at == 1900
        now[0] = 1900
        files.expire()
        assert list(files.directory.iterdir()) == []
        with pytest.raises(FileAccessError):
            with files.open(ALICE, info.handle):
                pass
    with pytest.raises(FileAccessError):
        await files.stage(ALICE, "a", "text/plain", chunks(b"hello"))


@pytest.mark.asyncio
async def test_local_path_cannot_escape_allowed_root(tmp_path: Path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "ok").write_bytes(b"local-data")
    (tmp_path / "outside").write_bytes(b"private")
    (allowed / "link").symlink_to(tmp_path / "outside")
    with FileStore(parent=tmp_path, allowed_root=allowed) as files:
        info = await files.import_local(ALICE, "ok")
        with files.open(ALICE, info.handle) as (_, stream):
            assert stream.read() == b"local-data"
        for path in ("../outside", str(tmp_path / "outside"), "link", "."):
            with pytest.raises(FileAccessError):
                await files.import_local(ALICE, path)
    with FileStore(parent=tmp_path) as files:
        with pytest.raises(FileAccessError):
            await files.import_local(ALICE, "ok")


@pytest.mark.asyncio
async def test_interrupted_upload_is_removed(tmp_path: Path):
    async def broken():
        yield b"partial"
        raise RuntimeError("disconnected")

    with FileStore(parent=tmp_path) as files:
        with pytest.raises(RuntimeError):
            await files.stage(ALICE, "a", "text/plain", broken())
        assert list(files.directory.iterdir()) == []


@pytest.mark.asyncio
async def test_total_storage_and_handle_limits(tmp_path: Path):
    with FileStore(parent=tmp_path, max_total_bytes=5, max_files=2) as files:
        await files.stage(ALICE, "a", "text/plain", chunks(b"abc"))
        with pytest.raises(FileAccessError, match="storage"):
            await files.stage(BOB, "b", "text/plain", chunks(b"def"))
        await files.stage(BOB, "empty", "text/plain", chunks(b""))
        with pytest.raises(FileAccessError, match="handle"):
            await files.stage(ALICE, "empty", "text/plain", chunks(b""))


@pytest.mark.asyncio
async def test_shutdown_during_stream_cannot_publish_handle(tmp_path: Path):
    with FileStore(parent=tmp_path) as files:

        async def interrupted():
            yield b"data"
            files.close()

        with pytest.raises(FileAccessError):
            await files.stage(ALICE, "a", "text/plain", interrupted())


@pytest.mark.asyncio
async def test_special_local_files_and_header_injection_are_rejected(tmp_path: Path):
    import os

    (tmp_path / "dir").mkdir()
    os.mkfifo(tmp_path / "pipe")
    with FileStore(parent=tmp_path, allowed_root=tmp_path) as files:
        for name in ("dir", "pipe", "bad\x00name"):
            with pytest.raises(FileAccessError):
                await files.import_local(ALICE, name)
        with pytest.raises(FileAccessError):
            await files.stage(ALICE, "bad\r\nname", "text/plain", chunks(b"x"))
        with pytest.raises(FileAccessError):
            await files.stage(ALICE, "a", "text/plain\r\nX-Evil: true", chunks(b"x"))


def test_file_limits_from_environment(monkeypatch):
    from thinkhome_zulip_mcp.files import file_store_from_environment

    monkeypatch.setenv("MCP_FILE_MAX_BYTES", "10")
    monkeypatch.setenv("MCP_FILE_MAX_TOTAL_BYTES", "20")
    monkeypatch.setenv("MCP_FILE_MAX_HANDLES", "2")
    with file_store_from_environment() as files:
        assert (files.max_bytes, files.max_total_bytes, files.max_files) == (10, 20, 2)
        assert files.allowed_root is None
    for invalid in ("0", "-1", "invalid"):
        monkeypatch.setenv("MCP_FILE_MAX_BYTES", invalid)
        with pytest.raises(ValueError):
            file_store_from_environment()


@pytest.mark.asyncio
async def test_background_expiration_removes_files(tmp_path, monkeypatch):
    import asyncio

    import thinkhome_zulip_mcp.files as module

    now = [0.0]
    calls = 0

    async def tick(seconds):
        nonlocal calls
        assert seconds == 30
        calls += 1
        if calls == 1:
            now[0] = 901.0
        else:
            raise asyncio.CancelledError

    with FileStore(parent=tmp_path, clock=lambda: now[0]) as files:
        await files.stage(ALICE, "a", "text/plain", chunks(b"abc"))
        monkeypatch.setattr(module.asyncio, "sleep", tick)
        with pytest.raises(asyncio.CancelledError):
            await files.cleanup_loop()
        assert list(files.directory.iterdir()) == []

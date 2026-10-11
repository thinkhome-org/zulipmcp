"""Ephemeral principal-owned files; paths are never accepted by HTTP callers."""

from __future__ import annotations

import asyncio
import mimetypes
import os
import secrets
import stat
import tempfile
import time
from collections.abc import AsyncIterable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

if TYPE_CHECKING:
    from thinkhome_zulip_mcp.executor import Principal


class FileAccessError(ValueError):
    pass


class FileLimitError(FileAccessError):
    pass


@dataclass(frozen=True)
class FileInfo:
    handle: str
    filename: str
    content_type: str
    size: int
    expires_at: float


@dataclass(frozen=True)
class _Entry:
    info: FileInfo
    owner: tuple[str, str]
    path: Path


class FileStore:
    def __init__(
        self,
        *,
        parent: Path | None = None,
        allowed_root: Path | None = None,
        max_bytes: int = 25 * 1024 * 1024,
        clock: Callable[[], float] = time.time,
        max_total_bytes: int = 100 * 1024 * 1024,
        max_files: int = 100,
    ) -> None:
        if min(max_bytes, max_total_bytes, max_files) <= 0:
            raise ValueError("File size limit must be positive")
        self._temporary = tempfile.TemporaryDirectory(prefix="zulip-mcp-", dir=parent)
        self.directory = Path(self._temporary.name)
        self.allowed_root = allowed_root.resolve(strict=True) if allowed_root else None
        self.max_bytes = max_bytes
        self.clock = clock
        self.max_total_bytes = max_total_bytes
        self.max_files = max_files
        self._pending_files = 0
        self._pending_bytes = 0
        self._entries: dict[str, _Entry] = {}
        self._closed = False

    def __enter__(self) -> FileStore:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._closed = True
        self._entries.clear()
        self._temporary.cleanup()

    @staticmethod
    def _owner(principal: Principal) -> tuple[str, str]:
        return principal.identity_id, principal.realm_url.rstrip("/")

    def expire(self) -> None:
        for handle, entry in list(self._entries.items()):
            if entry.info.expires_at <= self.clock():
                entry.path.unlink(missing_ok=True)
                del self._entries[handle]

    async def stage(
        self,
        principal: Principal,
        filename: str,
        content_type: str,
        chunks: AsyncIterable[bytes],
    ) -> FileInfo:
        if self._closed:
            raise FileAccessError("File store is closed")
        if (
            not filename
            or len(filename) > 255
            or filename in {".", ".."}
            or any(ord(c) < 32 or c in "/\\" for c in filename)
        ):
            raise FileAccessError("Invalid filename")
        if (
            not content_type
            or len(content_type) > 256
            or any(ord(c) < 32 or ord(c) > 126 for c in content_type)
        ):
            raise FileAccessError("Invalid content type")
        self.expire()
        if len(self._entries) + self._pending_files >= self.max_files:
            raise FileLimitError("File handle limit exceeded")
        self._pending_files += 1
        reserved = 0
        handle = secrets.token_urlsafe(32)
        path = self.directory / handle
        size = 0
        complete = False
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output:
                async for chunk in chunks:
                    if self._closed:
                        raise FileAccessError("File store is closed")
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise FileLimitError("File size limit exceeded")
                    stored = sum(entry.info.size for entry in self._entries.values())
                    if stored + self._pending_bytes + len(chunk) > self.max_total_bytes:
                        raise FileLimitError("File storage limit exceeded")
                    self._pending_bytes += len(chunk)
                    reserved += len(chunk)
                    output.write(chunk)
            if self._closed:
                raise FileAccessError("File store is closed")
            info = FileInfo(handle, filename, content_type, size, self.clock() + 900)
            self._entries[handle] = _Entry(info, self._owner(principal), path)
            complete = True
            return info
        finally:
            self._pending_files -= 1
            self._pending_bytes -= reserved
            if not complete:
                path.unlink(missing_ok=True)

    async def cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            self.expire()

    def _entry(self, principal: Principal, handle: str) -> _Entry:
        self.expire()
        entry = self._entries.get(handle)
        if self._closed or entry is None or entry.owner != self._owner(principal):
            raise FileAccessError("File handle unavailable")
        return entry

    def remove(self, principal: Principal, handle: str) -> None:
        entry = self._entry(principal, handle)
        entry.path.unlink(missing_ok=True)
        del self._entries[handle]

    @contextmanager
    def open(
        self, principal: Principal, handle: str
    ) -> Iterator[tuple[FileInfo, BinaryIO]]:
        entry = self._entry(principal, handle)
        with entry.path.open("rb") as source:
            yield entry.info, source

    async def import_local(self, principal: Principal, relative_path: str) -> FileInfo:
        root = self.allowed_root
        parts = Path(relative_path).parts
        if (
            root is None
            or not parts
            or Path(relative_path).is_absolute()
            or any(p in {".", ".."} for p in parts)
            or "\\" in relative_path
            or any(ord(char) < 32 for char in relative_path)
        ):
            raise FileAccessError("Path outside allowed root")
        # Walk descriptors without following symlinks, including during rename races.
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            try:
                for part in parts[:-1]:
                    next_fd = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=directory_fd,
                    )
                    os.close(directory_fd)
                    directory_fd = next_fd
                fd = os.open(
                    parts[-1],
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory_fd,
                )
            except OSError:
                raise FileAccessError("Local file unavailable") from None
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                raise FileAccessError("Local input must be a regular file")
            with os.fdopen(fd, "rb") as source:

                async def chunks() -> AsyncIterable[bytes]:
                    while chunk := source.read(65536):
                        yield chunk

                content_type = (
                    mimetypes.guess_type(parts[-1])[0] or "application/octet-stream"
                )
                return await self.stage(principal, parts[-1], content_type, chunks())
        finally:
            os.close(directory_fd)


def file_store_from_environment(allowed_root: Path | None = None) -> FileStore:
    return FileStore(
        allowed_root=allowed_root,
        max_bytes=int(os.environ.get("MCP_FILE_MAX_BYTES", 25 * 1024 * 1024)),
        max_total_bytes=int(
            os.environ.get("MCP_FILE_MAX_TOTAL_BYTES", 100 * 1024 * 1024)
        ),
        max_files=int(os.environ.get("MCP_FILE_MAX_HANDLES", 100)),
    )

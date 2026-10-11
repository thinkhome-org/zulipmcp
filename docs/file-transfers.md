# File transfer backend (development)

The internal `FileStore` and multipart executor adapter are implemented. Public
HTTP staging/download routes, stdio staging tools, signed URL downloads and
server-managed cleanup are the next integration step. Existing CLI configurations
do not yet create a FileStore. This document describes the backend contract, not
a ready-to-use client upload flow.

## Ownership and bounds

A staged file has a random handle, filename, MIME type, byte count and expiration.
The owning `(identity_id, realm_url)` comes from the authenticated Principal.
Callers must never construct that owner from model arguments. Unknown, expired and
other-user handles are refused before an upstream request. Files expire after
15 minutes; `open()` checks expiration and `expire()` removes expired files.

Default limits are 25 MiB per file, 100 MiB stored/in-flight bytes in total, and
100 stored/in-flight handles. Enforcement happens while receiving chunks, and a
failed stream removes its partial file and releases quota. Filenames and content
types reject header-control characters. Bytes are stored unchanged in mode-0600
files under a private temporary directory. Filenames never become storage paths.
Use `close()`/the context manager to remove the store at shutdown. Background
expiration and graceful server shutdown integration are still pending; merely
creating the backend does not schedule a cleanup task.

## Local import

`import_local()` is disabled without an explicit allowed root. It accepts only
relative paths, walks directory file descriptors without following symlinks,
rejects parent traversal, and reads only regular files. Directories, FIFOs and
symlink targets are refused. The copied data passes through the same streaming
limits as an HTTP upload. This implementation targets POSIX hosts (Linux/macOS).

## Multipart execution

Provide a FileStore to `Executor(files=...)`. A binary operation parameter receives
an opaque staged handle, never a host path or base64 payload. Seven catalog
operations currently expose multipart definitions:

| Operation | Binary parameter |
|---|---|
| `upload-file` | `filename` |
| `upload-custom-emoji` | `filename` |
| `upload-own-avatar` | `file` |
| `upload-realm-icon` | `file` |
| `upload-realm-logo` | `file` |
| `create-bot` | `file` |
| `update-bot` | `file` |

Other fields retain their declared form/JSON encoding, including logo `night`
and bot webhook URLs. Bot create/update continue to use regular forms when no
file is supplied. Handles can be reused until expiration; successful upload does
not delete them. Writes retain the executor's no-automatic-retry policy and
uncertain-write reporting.

Tests verify ownership, traversal, symlinks, nonregular inputs, quotas, interrupted
streams, expiry, shutdown during staging, and exact multipart bytes/method/path.
They do not prove upstream MIME/image acceptance, all optional parameters, or a
live Zulip upload. The complete API coverage gate remains false.

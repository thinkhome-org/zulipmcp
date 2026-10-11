# File transfer backend (development)

HTTP staging, retrieval/deletion of staged files, stdio local import and multipart
execution are implemented. Existing Zulip attachments can be copied into a staged
handle with `zulip_download_file(realm_id_str, filename)`.

## Client flow

HTTP clients use the same bearer token as MCP:

| Request | Result |
|---|---|
| `POST /mcp/files?filename=example.png` with raw bytes and `Content-Type` | 201 JSON: handle, filename, content_type, size, expires_at |
| `GET /mcp/files/{handle}` | Owner-only attachment bytes |
| `DELETE /mcp/files/{handle}` | 204; invalid/expired/other-owner handles return 404 |

The prefix follows the configured `MCP_RESOURCE_URL` path. Uploads are bounded
while streaming and time out after 60 seconds. Oversized uploads return 413.
Host/Origin checks and token scope/revocation checks also apply to these routes.
Pass the returned handle to the binary parameter of `zulip_execute`.

For stdio, set `ZULIP_FILE_ROOT` to an allowed directory before starting the server.
This enables `zulip_stage_file(path)` with a relative path under that directory.
HTTP never exposes this local-path tool. Without an allowed root, stdio local
staging is disabled.

Optional positive-integer environment limits: `MCP_FILE_MAX_BYTES`,
`MCP_FILE_MAX_TOTAL_BYTES`, `MCP_FILE_MAX_HANDLES`. Defaults are listed below.
`zulip_capabilities` reports the active limits and staging path.

## Downloading an existing Zulip attachment

For `/user_uploads/1/ab/hash/example.png`, call `zulip_download_file` with
`realm_id_str=1` and `filename="ab/hash/example.png"`. The caller's Zulip API key
requests `get-file-temporary-url`; Zulip decides whether that caller may access
it. A separate request downloads the signed URL without API credentials or
cookies. The result contains only staged-file metadata, never the signed URL.
HTTP callers retrieve bytes using the owner-bound route above; staged handles
can also be passed to multipart operations in either transport.

By default, download requests stay on the configured Zulip origin. For S3/CDN
redirects, set `MCP_DOWNLOAD_ORIGINS` to a comma-separated list of exact HTTPS
origins, for example `https://my-bucket.s3.example,https://files.example:8443`.
No wildcard, credentials, path, query or fragment is accepted in this setting.
Every redirect is checked, cookies are discarded between hops, at most four file
requests are made, and transfers have a 60-second overall timeout. HTTP is allowed
only for the explicitly enabled insecure test realm, never an external origin.
Streaming size/storage limits and failure cleanup also apply to downloads.

The initial temporary URL format is audited against Zulip 12.3
`zerver/views/upload.py:215-223,251-290,382-427`. Nested filename segments are
supported for this operation and `check-thumbnail-status` (the two core API
`path:filename` routes at `zproject/urls.py:464-472`); traversal, encoded path
separators and empty segments are rejected. Redirect/storage behavior is covered
by fixtures, not a live Zulip/S3 deployment. Administrators must configure trusted
storage origins matching their deployment.

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
The server removes expired files every 30 seconds and closes the store during
graceful shutdown. Expired handles are rejected immediately on access. Abrupt
termination cannot run cleanup; host temporary-directory cleanup must remove
abandoned files. Standalone backend callers must use `close()` or the context
manager themselves.

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

# ThinkHome Zulip MCP

In development. stdio and authenticated multi-user HTTP share a validated
JSON/form executor. Full API coverage and production readiness remain unverified.

The accepted scope is all callable application API operations of a pinned Zulip
release, including audited undocumented operations, stdio, and authenticated
multi-user Streamable HTTP. No custom frontend or Zulip host administration.

## Local stdio

Provide your own Zulip identity through environment configuration. Put its API key
in a file outside the repository; do not place it in tool arguments or Git.

```sh
ZULIP_URL=https://zulip.example \
ZULIP_EMAIL=you@example.invalid \
ZULIP_API_KEY_FILE=/absolute/path/to/key \
uv run thinkhome-zulip-mcp serve --transport stdio
```

`ZULIP_API_KEY` is an alternative environment variable; setting both sources is an
error. HTTPS is required by default. `ZULIP_ALLOW_INSECURE_HTTP=1` is an explicit
opt-in for a configured local/test instance.

Tools: `zulip_search_operations`, `zulip_describe_operation`, `zulip_execute`,
`zulip_capabilities`. Search defaults to 20 entries (maximum 100); descriptions
include input schemas and referenced definitions. The configured identity is
never a tool argument. Zulip decides current permissions on each call.

The executor rejects unknown arguments, method overrides and unsafe path values.
It returns structured errors and an `uncertain_write` flag; a failed write must
not be assumed rolled back. Redirects are returned without following them.
Responses are limited to 8 MiB by default. Up to three attempts are allowed only
for explicitly audited safe reads; a `Retry-After` longer than 30 seconds is
returned to the client without retrying earlier. Writes are never retried.

Multipart uploads support staged handles through authenticated HTTP routes or
stdio allowed-root import; see [file transfers](docs/file-transfers.md).
Existing attachments can be downloaded into owned handles; external storage
redirects require configured HTTPS origins. Alternative authentication remains pending.
No live Zulip or production operation has been verified; protocol tests use
disposable local HTTP and ASGI fixtures.

Set `ZULIPCHAT_READ_ONLY=true` to enforce read-only Zulip access across tools
and batches. See [access policies and audit limitations](docs/access-policies.md).

## Identity provisioning

Local administrator commands now manage encrypted identities, expiring manual
MCP tokens and offline key rotation. See [provisioning and recovery](docs/identity-provisioning.md).
Run `serve --transport http` with manual MCP tokens or external OAuth JWTs; see
[HTTP configuration and limits](docs/http-auth.md). The [Keycloak integration fixture](docs/keycloak-testing.md) exercises real PKCE
and network MCP clients. Actual Zulip integration remains pending.

## Development

Requires Python 3.12+ and uv.

```sh
uv sync --frozen
uv run thinkhome-zulip-mcp --version
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts
uv run pytest
uv run python scripts/catalog.py check
uv build
```

See [CONTRIBUTING](CONTRIBUTING.md), the
[design](docs/superpowers/specs/2026-10-11-thinkhome-zulip-mcp-design.md), and the
[implementation plan](docs/superpowers/plans/2026-10-11-thinkhome-zulip-mcp.md).

Original project code is licensed under [MIT](LICENSE). Bundled Zulip material
and adapted windborne Markdown helpers retain Apache-2.0; see
[third-party notices](src/thinkhome_zulip_mcp/data/THIRD-PARTY-NOTICES.md).
Publication targets remain pending; no release is published.

## Required reference parity

The release target includes every non-Hermes feature of `windborne/zulipmcp`,
`unfoldingWord/uw-zulip-mcp` and `akougkas/zulipchat-mcp` **plus**
the complete audited Zulip application API. See the pinned
[windborne inventory](docs/zulipmcp-parity.md) and
[additional reference inventory](docs/additional-mcp-parity.md). This is a requirement and
tracking document, not a claim that those features are already implemented.

Managed event registration, bounded polling and deletion are documented in
[event queues](docs/events.md) and [bounded batch/history tools](docs/workflows.md).
Full reference listening/session behavior remains
in progress.

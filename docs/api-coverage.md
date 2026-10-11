# API coverage — work in progress

Target: Zulip **12.3**, commit `b31982251ff82d0b02e59f63ad21f19204e37aea`.
Upstream main observed at `d7c57ed15b82b0bc8aefe2007f5ee2abeebd0ea2` (2026-10-11).
The main revision is recorded, not yet audited.

The bundled OpenAPI is byte-for-byte upstream material with its Apache-2.0
license and SHA-256 in `data/provenance.json`. This upstream license does not
select the license for our own implementation.

## Current evidence

- 158 upstream schema operations after three documentation-only exclusions, plus
  29 source-reviewed additions, including the full core REST gap and public
  subdomain availability.
- Generic JSON/form execution is available. Wire tests cover selected operations;
  all-operation wire coverage and live Zulip integration coverage remain incomplete.
  Catalog implementation statuses are still provisional and must not be read as
  certification of the generic executor for every operation.
- The three documentation-only paths are listed in `data/exclusions.json`, with
  evidence from upstream `zerver/tests/test_openapi.py:241-249`.
- 187 is **not** a claim about total application API coverage. The schema includes
  development/bouncer APIs and does not describe all application operations.

## Outstanding audit

Inspect routes in `zproject/urls.py`, `zproject/tornado_urls.py`,
`zproject/dev_urls.py`, and included URL configurations; compare handlers,
authentication decorators, request validators and upstream tests. Dynamic routes,
SCIM, direct file routes, cookie-only operations and provider integrations need
explicit classifications. Preserve every request parameter and encoding.

Source inspection already identified undocumented operations including organization
updates, bot management and onboarding; their complete schemas, roles and session
requirements are not yet audited. They remain required work before v1.

No operation may be declared implemented merely because its metadata loads.

## Reproducible source evidence

`data/routes.json` contains 451 static route records from six upstream URL files,
359 handler signatures/decorator lists, and SHA-256 hashes of 90 source files.
These include HTML pages, optional server components and include declarations:
451 is not a count of callable API operations. Every route still requires final
classification; signatures alone do not account for indirect validation or permissions.
Dynamic route expansion, external packages such as SCIM, and handler bodies remain
part of the audit. Source files are parsed as Python syntax, never imported or run.

```sh
uv run python scripts/catalog.py check
uv run python scripts/catalog.py check --source-root /path/to/zulip-12.3
uv run python scripts/catalog.py update --source-root /path/to/zulip-12.3
uv run python scripts/catalog.py check --require-reviewed
```

The first command checks the pinned schema and local evidence consistency.
The second additionally reproduces the snapshot from supplied upstream files.
`update` rewrites generated evidence for review; it never declares routes reviewed.
`--require-reviewed` currently fails deliberately: pending audit and unimplemented
operations must block a complete release. A green ordinary check is not that gate.

## First reviewed undocumented additions

| Operation | Input/authentication evidence | Execution |
|---|---|---|
| PATCH `/settings/display` | Same human-user handler and full schema as `/settings` | Not implemented |
| PATCH `/settings/notifications` | Same human-user handler and full schema as `/settings` | Not implemented |
| POST `/users/me/onboarding_steps` | Human caller; step name and optional JSON-encoded reminder delay; source role and cross-field constraints retained | Not implemented |

Overlay source references, side effects and unknown feature levels are stored in
`data/overlays.json`. No integration verification is claimed. The onboarding
operation can schedule a welcome-bot reminder; it is not classified as read-only.

## Core route and parameter audit update

All 177 explicit methods declared with `rest_path` in the pinned main
`v1_api_and_json_patterns` now have catalog entries. Its direct public subdomain
GET route has a separate schema as well. Contract tests reconcile the core typed
handler keyword parameters against resolved OpenAPI references plus overlays.
This is metadata/input accounting, not HTTP serialization or integration coverage.

The 28 initial core REST gaps are now represented: realm settings (71 inputs),
branding, bots, profile fields, deactivation, widget submessages, slash commands,
message summaries, avatars, onboarding, settings aliases, default channel groups,
Zoom calls and export deletion. Separate source-backed additions cover the four
undocumented send-message parameters and the `subject` alias on all five handlers
using `OptionalTopic`, including typing notifications.

Important distinctions retained in schemas and metadata:

- Realm admin is insufficient for owner-only organization fields.
- `sender`/`forged` require the caller's pre-existing `can_forge_sender` capability;
  no credentials are substituted and the MCP never grants that capability.
- Bot creation/listing can return secrets. These outputs must not be logged.
- Image reads return redirects; outgoing credentials must never follow them
  across origins. Uploads require multipart; bot updates also accept a form
  when no avatar is supplied.
- Message summary uses GET but may spend AI credits, so it is not retry-safe.
- `topic` and legacy `subject` are mutually exclusive, not last-value-wins.
- Deactivation's JSON nullable delay differs from onboarding's optional but
  non-nullable JSON delay.

Still pending: non-core/included routes (SCIM, optional components, files and
session-specific routes), runtime/dynamic route expansion, global wrapper inputs,
response schemas and complete per-operation execution/integration certification. Generated route records
remain `pending` until their complete final classification is recorded. Do not
interpret source-reviewed input metadata as a completed route audit.

## Executor evidence (development)

The stdio process has passed MCP initialize/list/call tests and a complete
`zulip_execute` send-message call against a local disposable HTTP fixture. It is
not a live Zulip integration test. HTTPX boundary tests cover form/query encoding,
unknown fields, alias conflicts, authentication/cookie isolation, path traversal,
redirects, response bounds, strict JSON numbers, timeout uncertainty and bounded
safe-read retries. These tests do not certify the other catalog entries.

Multipart execution now accepts staged owner-bound handles for all seven upload
schemas (attachment, emoji, own avatar, realm icon/logo, bot create/update).
Contract tests verify method/path, representative encoded fields and exact file
bytes. These are HTTPX boundary checks, not complete parameter or live-handler
certification. Authenticated HTTP staging/retrieval/deletion, stdio allowed-root import and
server lifecycle cleanup are implemented. Downloading existing Zulip attachments
via signed URLs remains pending.

Other pending adapters include binary downloads, cookie/session authentication,
credentials-retrieval flows and server-to-server endpoints. Authenticated
multi-user HTTP and the real Keycloak PKCE/network fixture are implemented;
actual Zulip integration is still missing. The complete release gate remains false.

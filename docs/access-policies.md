# Access policies

## Read-only Zulip access

Set `ZULIPCHAT_READ_ONLY=true` (or `1`) before startup to reject upstream
mutations. `false` / `0` disable it; the default is false. Other values reject
startup to avoid silently running with writes enabled. Python callers may supply
`Executor(read_only=True)` explicitly.

The common executor rejects non-GET/HEAD/OPTIONS operations and operations marked
unsafe in catalog metadata. It also rejects GET endpoints that issue a signed BigBlueButton meeting join
capability, generate a summary, or acknowledge an event queue. The BigBlueButton
endpoint itself does not create the meeting at the provider.
`get-stream-email-address` is also blocked: the pinned Zulip handler calls
`get_channel_email_token`, which uses `ChannelEmailAddress.objects.get_or_create`
and can persist a new channel email address even though the HTTP method is GET. Denials
return `read_only_policy` before argument processing or network access. The same
check applies to direct tools, generic execution and every batch item.

Message writes and managed event lifecycle tools are removed from tool discovery.
Generic execution/batch remain available for reads; write attempts still fail.
Catalog search/descriptions retain the full inventory, including blocked
operations. `zulip_capabilities.read_only` reports the active mode.

This policy concerns Zulip application writes. Authenticated local file staging,
download staging and handle cleanup remain available; these change temporary MCP
storage. Creating an event queue is blocked, so read-only consumers use history
polling rather than the managed event tools. An API key's Zulip permissions are
still enforced upstream; read-only mode never grants additional access.

## Source audit evidence

Pinned Zulip source: `b31982251ff82d0b02e59f63ad21f19204e37aea`.

- `zerver/views/streams.py:get_stream_email_address` calls
  `zerver/lib/email_mirror_helpers.py:get_channel_email_token`, whose
  `get_or_create` can persist a channel email address: blocked.
- `zerver/views/video_calls.py:get_bigbluebutton_url` returns a signed join
  capability; provider meeting creation occurs later in the join handler:
  conservatively blocked.
- `zerver/views/upload.py:serve_file_url_backend` enters `serve_file` with
  `url_only=True` and returns before thumbnail generation: allowed.
- `zerver/views/thumbnail.py:check_thumbnail_status` queries thumbnail metadata:
  allowed. This does not certify thumbnail-serving routes, which can generate
  files or delete a failed image-attachment row.
- `zerver/views/users.py:get_bot_api_key` reads the key of an authorized bot:
  allowed. Read-only mode is not a secret-redaction policy.

These source checks cover the named handlers, not every middleware, helper or
provider side effect. Tests assert policy denials before network access, allowed
history reads, strict configuration parsing, batch enforcement and authenticated
HTTP discovery/execution. Real Zulip integration coverage remains pending.

## Channel policy implementation in progress

The executor now loads Johnny Decimal settings (`ZULIPCHAT_CHANNEL_FILTER_ENABLED`,
`ZULIPCHAT_JD_ALLOW_AREAS`, `ZULIPCHAT_JD_DENY_AREAS`,
`ZULIPCHAT_CHANNEL_INCLUDE`, `ZULIPCHAT_CHANNEL_EXCLUDE`,
`ZULIPCHAT_EXCLUDE_NON_JD`, `ZULIPCHAT_EXCLUDE_DMS`,
`ZULIPCHAT_EXCLUDE_PRIVATE`) and optional `BOT_ALLOWED_PRIVATE_STREAMS` /
`BOT_ALLOWED_WRITE_STREAMS` allowlists. Invalid booleans, ranges or JSON fail startup.
All filters are inactive by default. An explicitly empty private allowlist denies
private channels; an unset allowlist leaves ordinary API permissions unchanged.
The windborne compatibility profile's default-private-denial remains pending.

### Guarded operations

| Operations | Enforcement |
|---|---|
| `send-message` | Resolve and validate channel metadata, enforce read/write rules, send to the checked numeric ID; apply DM restriction for direct messages. |
| `get-streams` | Validate each channel and filter the list under the current caller. |
| `get-stream-id`, `get-stream-by-id` | Check current channel name/privacy before returning metadata or ID. |
| `get-messages` | Filter each page using current per-caller channel metadata and the DM rule; preserve traversal bounds for pagination. |
| `get-message` | Validate exact message ID, check current channel metadata or the DM rule before returning content. |
| `update-message`, `delete-message`, `add-reaction`, `remove-reaction` | Validate input, check source-message access and source-channel write rules; moves also check the destination. Read-only denials precede metadata requests. |
| `get-stream-topics`, `get-subscribers`, `get-subscription-status` | Check channel metadata before fetching the requested data. |

While a channel policy is active, all other operations return
`channel_policy_not_supported` without making an upstream call. This temporary
restriction prevents unguarded generic endpoints from bypassing the rules;
it is not complete reference parity. Single-message checks do not trust a
message's display name as evidence of privacy. Listings reject malformed results
and strip unfiltered data from upstream errors.

### Rule precedence

JD settings preserve the reference ordering: private exclusion and an excluded
name win; an included name then overrides JD area restrictions; otherwise a denied
area wins over the area allowlist. Non-JD names follow `EXCLUDE_NON_JD`. JD named
lists are case-sensitive. Private/write allowlists match lowercased names and are
additional constraints that a JD include cannot bypass. Allowlist syntax accepts
comma-separated names, a JSON string or a JSON string array; `__ALL__` removes
that allowlist restriction. An explicitly empty JSON write array denies channel
writes, while an empty/unset write string retains the reference's allow-all default.


Channel sends resolve names to IDs, fetch current channel metadata under the
same caller credentials, validate metadata and permissions, then send using the
checked numeric ID. Missing or denied metadata returns `channel_policy_denied`.
There is no shared metadata cache. A concurrent privacy/name change between check
and write cannot be made atomic by this MCP; Zulip's own permissions remain the
final authority. DMs follow the configured DM restriction.

## Remaining work

The complete endpoint source audit is still in progress. New endpoint additions
must be reviewed for side effects, including GET endpoints; an HTTP method alone
is insufficient evidence. Channel policies still need guards for reads and other mutations, response
filtering, event bypass tests and configurable agent/admin policies. This page does not claim full reference-policy parity.

## Filtered history pagination

When channel filtering is active, `get-messages` returns `policy_page` with the
first/last upstream message IDs and the original page count. These are pagination
metadata: they can include IDs of messages whose content was removed. This policy
filters message content and channel access; it does not conceal the existence or
number of underlying messages. Unexpected upstream fields and upstream-supplied
`policy_page` are discarded.

`zulip_history` advances using these bounds even when all messages were filtered
out. An empty visible page with `exhausted=false` means continue with `next_cursor`.
Metadata lookups use the current caller, cache only within one page and have a
30-second overall filtering deadline. Failed/malformed lookups fail the whole page
without content or a new cursor, so transient failures cannot silently skip data.

Concurrent changes to channel metadata or messages remain possible between calls;
this is not a consistent snapshot. Event filtering remains pending.

## Authenticated HTTP verification

The HTTP policy integration test initializes separate MCP sessions for two
provisioned callers. It checks generic and dedicated send denial, a batch that
stops after its first denied operation, per-caller history filtering and rejection
of currently unsupported event registration without upstream access. This uses
real MCP/ASGI authentication with a simulated Zulip transport; it is not live
Zulip verification or exhaustive coverage of all tool/endpoint combinations.

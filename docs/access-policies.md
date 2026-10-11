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

## Remaining work

The complete endpoint source audit is still in progress. New endpoint additions
must be reviewed for side effects, including GET endpoints; an HTTP method alone
is insufficient evidence. Private/write channel allowlists, Johnny Decimal
filters, denied-channel response filtering and configurable agent/admin policies
are not yet implemented. This page does not claim full reference-policy parity.

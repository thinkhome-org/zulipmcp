# Bounded workflows

`zulip_batch(operations)` accepts 1–50 objects with exactly `operation_id` and
`arguments`. Operations run sequentially through the same catalog executor as
`zulip_execute`, under the authenticated caller. Access is refreshed before each
operation, so token revocation stops subsequent operations.

The response contains indexed `results` (including the failed operation),
`completed` successful operations, and zero-based `not_attempted` indexes.
The first error stops the batch. `uncertain_write` is propagated; inspect the
individual response before deciding whether to retry. There is no rollback,
transaction, or additional automatic retry. Cancellation can leave earlier writes
committed and the current write uncertain; do not blindly replay a cancelled batch.

Malformed batch envelopes are rejected before starting. Operation-specific
validation happens in the executor when each operation is reached; an invalid
later operation does not undo earlier successful operations.

Other convenience workflows remain in progress. Tests
currently use isolated fixtures; live Zulip acceptance is pending.

## History

`zulip_history(narrow, cursor=null, direction="older", limit=100)` fetches one
page (maximum 200) through `get-messages`. Start without a cursor at the newest
messages, or use `direction="newer"` to start at the oldest. Messages within each
page are in increasing ID order. Reuse `next_cursor` with the same narrow and
direction until `exhausted` is true. The cursor message is excluded; gaps from
deleted or inaccessible messages are permitted. No server-side cursor state or
snapshot is created. Concurrent edits/deletions or changing the narrow can change
which messages match; exhaustion describes the current response only.

The original Zulip metadata, including `history_limited`, is retained. Exhaustion
does not promise access to history hidden by a plan or permissions. Invalid,
non-progressing responses produce an error instead of a looping cursor. Each call
reauthorizes under the current caller; a message ID grants no access by itself.

Anchor behavior follows pinned Zulip 12.3 `zerver/lib/narrow.py` (parse anchor,
one-sided range queries and post-processing), with `include_anchor=false`.

## Message editing and topic moves

- `zulip_edit_message(message_id, content)` normalizes outgoing Markdown before sending. Zulip
  enforces who may edit and its configured time limits.
- `zulip_move_messages(message_id, topic, stream="", propagate_mode="change_one")`
  requests notifications in both old and new topics. Optional `stream` is the
  destination channel name, resolved under the caller's identity before writing.
  Lookup failure prevents the move. Access is refreshed before the write.
- `zulip_resolve_topic(message_id, topic, propagate_mode="change_all")` silently
  sets the full topic name supplied by the caller. Include `✔ ` to resolve, or
  remove it to unresolve. Both notification flags are false.

Propagation accepts `change_one`, `change_later`, or `change_all`. These operations
can affect multiple messages; inspect the source and destination before moving.
Upstream permission errors and uncertain writes retain the standard executor
response. No automatic rollback is performed.

This implements the basic reference move/resolve semantics. Configured channel allowlists and other reference message policies
are still pending, so these tools do not yet establish full reference parity.

## Reading, sending and reactions

- `zulip_get_message(message_id, apply_markdown=false)` returns the accessible
  message with raw Markdown by default and supports empty topic names.
- `zulip_send_message(stream, topic, content)` accepts a channel name or ID.
- `zulip_send_direct_message(recipients, content)` accepts a nonempty list of user
  IDs or Zulip API email addresses, including group DMs.
- `zulip_add_reaction` and `zulip_remove_reaction` accept `message_id`,
  `emoji_name`, and optional `emoji_code` / `reaction_type` for exact custom emoji
  selection. They change the authenticated caller's reaction.

Sending and editing apply the reference Markdown fixes by default. Set
`ZULIPMCP_MARKDOWN_AUTOFIX=0` or `false` to preserve content unchanged. Dedicated
message tools enforce `ZULIP_MAX_MESSAGE_LENGTH` (default 10,000 characters)
before and after normalization. Set a positive integer before server startup;
invalid values reject startup. The limit is reported by `zulip_capabilities`.
Zulip still enforces its own limits. Configured channel policies remain pending. These tools never accept credentials, origin or sender overrides;
advanced authorized API options remain available through the catalog executor.

### History with channel filters

When channel filtering is enabled, a visible page can be empty while
`exhausted` is false. Continue with `next_cursor`: it uses the original page
boundary so filtered messages do not cause a loop or premature completion.
`policy_page` reports original first/last message IDs and count; those metadata
can reveal that filtered messages exist, but do not include their contents.
See [access-policy limits](access-policies.md#filtered-history-pagination).

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

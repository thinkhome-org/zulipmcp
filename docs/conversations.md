# Conversation workflow (in development)

The current branch implements `zulip_set_context`, `zulip_reply`, `zulip_listen`
and `zulip_end_session`. The complete reference parity milestone is not yet
certified. Handles are ephemeral and owned by the authenticated identity and
Zulip realm; they cannot transfer another caller's session or credentials.

## History and delivery

Context records a channel, topic and message cursor. Reply fetches intervening
history before sending. If another history page is required, it returns
`catchup_required` without sending; consume the returned messages and continue.
An unsuccessful or uncertain send does not advance the history cursor and is
never retried automatically. A successful send records its message ID separately
so messages arriving between history retrieval and sending remain discoverable.

Listen registers its event queue before fetching history. An expired queue is
registered again, followed by another history fetch. `queue_renewed` reports this
transition. `history_limited=true` and `resync_required=true` report an upstream
history gap even if a later fetch is complete, a new event arrives, or the wait
times out. These flags do not promise that unavailable history can be recovered.

## Access and cleanup

Typing uses the same channel write restrictions and direct-message policy as
the executor. Reaction events require access to their target messages; queue
narrows alone do not establish access or topic membership. Failed metadata
lookups do not acknowledge an event batch.

Listen may subscribe the caller to an existing public channel. Private channels
require existing membership. With channel policy enabled, subscription checks
every requested channel before writing and applies the write allowlist. Missing
channels are rejected. Zulip's subscription API takes names, not immutable IDs:
a concurrent rename can still cause a channel creation or changed subscription.
A post-write ID check reports `subscription_target_changed` with
`uncertain_write=true`; it cannot undo that race and does not retry the write.

End cancels an active listen or reply before removing the session. Cleanup stops
typing and attempts queue deletion and removal of the listening indicator.
Shutdown cleanup is bounded; an unreachable Zulip server can prevent cleanup.
An interrupted send may already have reached Zulip and must not be blindly
retried.

## Verification limits

Unit, authenticated ASGI and real loopback MCP transport fixtures exercise the
above behavior, including progress and ending an active listen without losing
the MCP session. The upstream Zulip remains simulated. Full reference config/
formatting parity and live Zulip verification remain pending. No two-hour wall-clock
soak has been performed. This document is not a v1.0 completion claim.

## Trusted library configuration

`create_server` and `create_http_app` accept `conversation_options` from the
operator's Python configuration, not from tool arguments. Options currently
include `max_sessions`, `prefix` (string or callable), `auto_typing_start`,
`dismiss_emoji`, `on_set_context`, `on_reply`, and `on_session_end`. Limits must
be positive integers, typing suppression must be a boolean, and hooks must be
callable. Invalid configuration raises `ValueError` before sessions are created.

`dismiss_emoji` accepts a single emoji name or a set of names. Surrounding colons
are stripped (`:stop_sign:` becomes `stop_sign`); an empty set disables reaction
based dismissal. The manager copies the set, so later mutation of the supplied
set cannot change the running configuration. Hooks and prefix callbacks are
trusted application code and must not be constructed from model-supplied text.

`timezone` selects an IANA timezone (default `UTC`) for `formatted_messages`.
Context and listen return that XML representation alongside raw messages; reply
returns it alongside `missed_messages`. Message text and attributes are escaped.
Consecutive channel messages combine only for the same sender ID, channel ID and
case-insensitive topic, with a forward gap of at most 120 seconds. The formatter
preserves its inputs and includes compact reaction counts. Library helpers `format_topics` and `context_window` provide channel/topic grouping
and participant context selection. Grouping uses channel IDs, and participant
selection uses sender IDs. Context includes at least three messages on either
side, extends backward ten minutes, and extends forward up to two hours with
fewer than ten following messages. Connecting these helpers to advanced search
tools remains pending.

The formatter rejects invalid XML characters, malformed reaction containers and
unrepresentable timestamps. A formatting failure during listen or reply returns
`invalid_conversation_response` without advancing the message cursor or sending
an outgoing message.

## Local interrupt file

For stdio, trusted `conversation_options["interrupt_file"]` may point to a local
file. Listen checks it before each poll and limits polling to one second while
this feature is enabled. An interrupt returns `interrupted=true` and
`interrupt_content`, then performs ordinary queue/indicator cleanup. It keeps the
conversation available for a subsequent reply or listen. Empty content is also
an interrupt. No model tool argument can choose a filesystem path.

The consumer atomically renames the file to a unique `.claimed` path, reads at
most 64 KiB from a regular file without following symbolic links, and removes a
successfully read claim. New content at the original path is left for the next
call. Invalid UTF-8 is replaced. Failed claims remain on disk for inspection and
return `interrupt_file_error`; they are not automatically retried. A process
crash after claiming can strand the claim, so this is at-most-once delivery.
Writers should atomically replace the configured path instead of modifying an
already-open file. Shared interrupt files are rejected in authenticated HTTP
mode. Concurrent local listeners compete for a single interrupt; it is not a
broadcast mechanism.

Authenticated HTTP integration tests now verify separate Alice/Bob conversation
handles, rejection of cross-owner reply/listen/end without upstream requests,
caller-specific writes, token revocation before a call, and revocation between
profile lookup and the next context-setup request. These tests exercise the
ASGI MCP endpoint and real authentication store with a simulated Zulip transport;
they are not live Zulip or real-network long-poll verification.

## Local exit markers

Trusted stdio configuration may set `workspace_path` to an existing directory.
Explicit end and graceful shutdown write `.clean_exit` (Unix timestamp) and,
when a reply was sent, `.last_sent_message_id`. A session without a reply removes
an old last-message marker. Each marker is replaced atomically using a temporary
file in that directory; an existing marker symlink is replaced rather than
followed. HTTP rejects this shared workspace configuration.

Markers describe the latest local session end and are written before any farewell
request. They do not prove that a farewell was delivered. Multiple sessions using
the same workspace overwrite the same markers, and the pair is not an atomic
transaction. Use separate workspaces/processes when each session needs independent
markers. File errors return `exit_marker_failed` in warnings while normal session
cleanup continues; no directory is created implicitly.

## Automatic stdio context

Set both `SESSION_STREAM` and `SESSION_TOPIC` to initialize a local conversation
at server lifespan startup. An empty topic is supported; the channel must not be
blank. Optional `TRIGGER_MESSAGE_ID` must be a positive integer and is verified
against that channel/topic. `WORKSPACE_PATH` supplies the local marker directory
unless `conversation_options.workspace_path` explicitly overrides it.

Call `zulip_set_context` without arguments to inspect the automatic context and
its initial history. Reply/listen/end may omit `handle` to use it. Initialization
is serialized and reused; a failed initialization is returned by the context
call, and other implicit calls report failure. After ending it, explicitly create
and use a new context rather than assuming automatic recreation. Explicit handles
continue to work independently. HTTP ignores these environment settings and
requires explicit per-caller conversation handles. Incomplete stdio environment
configuration fails startup.

HTTP checks also cover token revocation while polling: newly received messages
are withheld and owned queue/indicator cleanup is attempted. Disconnecting an
HTTP request does not by itself mean the MCP operation was cancelled. Explicit
`zulip_end_session` cancels the active conversation operation without terminating
the enclosing MCP session, including after its original HTTP request disconnects.
Internally cancelled reply/listen calls return `conversation_cancelled` when their
response transport is still available. Network delivery of that response is not
guaranteed after a disconnect.

## HTTP progress transport

Streamable HTTP now defaults to streaming responses, which carry progress
notifications back to the client while `zulip_listen` waits. A real loopback TCP
fixture exercises the official MCP client, bearer authentication, progress,
message delivery and queue cleanup; the Zulip upstream remains simulated.

Library callers can explicitly select `create_http_app(..., json_response=True)`
for single-JSON compatibility. That SDK mode drops request-scoped notifications,
so it cannot deliver listen progress. Existing low-level ASGI fixtures explicitly
exercise this compatibility mode; the network progress fixture exercises the
streaming default. This is Streamable HTTP, not the still-pending legacy SSE
transport.

A simulated-clock test covers a complete requested two-hour listening interval:
288 polls of at most 25 seconds, progress reaching the requested duration, and
queue cleanup on timeout. This verifies loop bounds, not a two-hour network soak.

Cancellation while awaiting a reply send returns `uncertain_write=true`: Zulip
may already have accepted it. The operation is never retried automatically.
If Zulip has already confirmed the send, cancellation of the subsequent hook or
typing update preserves `sent_message_id` and returns `conversation_cancelled`
with `uncertain_write=false`. The message was sent; do not resend it.

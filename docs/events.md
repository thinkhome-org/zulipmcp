# Managed event queues

The stdio and authenticated HTTP servers expose three caller-scoped tools. They
reuse catalog validation and the same Zulip identity as ordinary API execution.

| Tool | Arguments | Result |
|---|---|---|
| `zulip_register_events` | `arguments`: validated `register-queue` inputs | Initial upstream state, `last_event_id`, opaque `handle` |
| `zulip_poll_events` | `handle`, `last_event_id`, optional `wait_seconds` (0–25, default 25) | `events`, `next_event_id`, `timed_out`, `resync_required` |
| `zulip_delete_events` | `handle` | Cancels a pending poll and deletes the queue |

For example, register only the event types and topic needed:

```json
{
  "arguments": {
    "event_types": ["message", "reaction"],
    "narrow": [["stream", "engineering"], ["topic", "example"]]
  }
}
```

Use the returned handle and initial `last_event_id` in the first poll. Process
and durably record received events before supplying `next_event_id` as the next
call's `last_event_id`. The manager does **not** acknowledge events automatically
when returning a response: a lost MCP response must not silently advance the
client cursor. IDs may have gaps; they must be increasing. Repeating a cursor
lets Zulip redeliver unacknowledged events.

A zero-second wait asks Zulip for a nonblocking response. Other calls wait at most
25 seconds, independently of Zulip's longer heartbeat interval. A local timeout
returns no events, the unchanged cursor and `timed_out=true`; it does not mean
that the queue expired. Cancellation stops the pending request and releases the
single-poll lock without advancing a cursor. A second concurrent poll on the
same handle returns `queue_busy`.

## Expiration, identity and lifecycle

- Handles belong to `(identity_id, realm_url)`; another user or realm receives
  `invalid_queue_handle` without contacting Zulip.
- HTTP authentication/revocation is checked for every new tool call. Each call
  resolves current credentials; there is no admin fallback.
- Zulip's `BAD_EVENT_QUEUE_ID` becomes `queue_expired` with
  `resync_required=true`. The invalid handle is removed. The caller must create
  a new queue and synchronize history/state; registration alone cannot recover
  the missing interval. No automatic re-registration hides that gap.
- Unknown handles after process restart also require synchronization. Managed
  queues are ephemeral and are not persisted to the identity database.
- The instance allows 100 managed handles, including registrations in flight.
  Call delete when finished; failed deletes retain the handle for retry. These
  limits apply to managed tools, not raw catalog calls to the upstream queue API.
- Graceful shutdown cancels all managed polls and attempts queue deletion within
  five seconds using each queue's latest caller credentials. Unreachable queues,
  interrupted registration with an unknown server ID, and abrupt process death
  rely on Zulip's server-side idle expiry. No successful cleanup is claimed when
  the network prevented it.

The low-level `register-queue`, `get-events` and `delete-queue` operations remain
available through `zulip_execute`. Their raw queue IDs do not become managed
handles automatically.

## Coverage and remaining work

Tests cover ownership across users and realms, explicit cursors, expiration,
concurrent registration/poll limits, cancellation, malformed responses, delete
retry, bounded shutdown, actual SDK tool dispatch, HTTP token revocation and
method/path/form/query encoding against an HTTP fixture.

This is the event-queue foundation. The reference `listen` workflow still needs
session context, catch-up, automatic subscription, listening/dismiss reactions,
renewable long waits, progress notifications and interrupt-file support. Live
Zulip event delivery and the complete reference parity gate remain unverified.

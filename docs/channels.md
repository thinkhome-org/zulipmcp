# Channel management (in progress)

See [the channel API audit](channel-api-audit.md) for the pinned operation and
parameter inventory, test evidence map, and remaining implementation gaps.

The `channels.channel_info` helper resolves a channel name or reads an explicit
ID using the caller's public Executor. When both are supplied, they must resolve
to the same channel. This does not elevate metadata access to content access.

Optional subscriber/topic reads report per-section errors in `errors` and mark
`partial`. Subscriber IDs are validated and sorted. Topics are ordered by latest
message ID descending; `topic_limit`, `topic_offset`, `topic_count` and
`next_topic_offset` expose bounded continuation, including empty topic names.
Offsets refer to live lists; concurrent changes may shift positions.

Every dependent request rechecks the original caller identity and realm. A
changed or revoked identity discards all accumulated results. No credentials or
origin can be passed through helper arguments.

`zulip_get_channel(include_member_profiles=true)` also reads subscribers and
joins their IDs to the caller-visible user directory. It returns complete
available records in `member_profiles`, including inactive users and bots,
and lists missing IDs in `unresolved_subscriber_ids`. Missing directory records
do not remove subscriber IDs or invent profiles. Directory failures are explicit
optional-section errors; malformed or duplicate user IDs reject that section.
The directory is fetched once per call and never cached across callers.

The read-only MCP tools `zulip_get_channel` and `zulip_list_channels` expose
these helpers with typed inputs. Current evidence uses unit fixtures and mocked
HTTP through the real Executor and MCP tool calls. The independent channel
review identified three authorization/isolation defects, now covered by regression
cases. The completed `uv run pytest` regression passed 786 tests in 871.57 seconds
(recorded 2026-10-11). Live Zulip verification remains pending. This is
not complete reference parity or API certification.

`channels.list_channels` forwards all nine Zulip channel-directory switches:
`include_public`, `include_subscribed`, `include_web_public`, `exclude_archived`,
`include_all`, legacy `include_all_active`, `include_default`,
`include_owner_subscribed` and `include_can_access_content`. Defaults match the
pinned API. Inclusion flags are combined by Zulip, so excluding the public list
does not remove public channels selected by the subscription flag. Metadata
access via `include_all` does not confer message access.

Results retain full channel objects, sort by stable channel ID, and support
`limit` (1–1000), `offset`, `total_count`, `next_offset` and `exhausted`. These are
live directory offsets, not snapshot guarantees. Duplicate or malformed IDs
produce an explicit error. Identity is checked again after the directory read.
Actual Executor serialization is covered with mocked HTTP for directory flags,
caller credentials, name resolution, metadata, members and empty-name topics;
this is not a live Zulip result. Separate MCP call tests cover read-only tool
registration and execution.

The list tool also returns `formatted` text for the current page. Optional
`fields` (up to 100 field names) selects extra presentation values, without
removing anything from `streams`. Missing fields render as `None`, numeric
`date_created` values as UTC ISO dates, and nested values as compact JSON capped
at 80 characters. Invalid dates fall back to their original representation.
Channel names/descriptions and selected values are untrusted Zulip content.

## Subscription property changes

`zulip_update_subscription_property(stream_id, property, value)` supports color,
muting (`is_muted` and the legacy inverse `in_home_view`), pinning and desktop,
audible, push, email and wildcard-mention notification settings. Color uses a
string validated by Zulip; other properties require an actual boolean. The MCP
tool rejects mismatched value types before any request.

The shared Executor checks the target channel against channel visibility and the
write allowlist, including calls made through generic `zulip_execute`. Read-only
mode hides the dedicated tool and rejects the operation before upstream access.
The request changes only the authenticated caller's subscription. Responses keep
normal uncertainty/error reporting; the write is never automatically retried.
Tests use mock HTTP only, including allowed/denied targets and read-only denial.

`zulip_update_subscription_settings(subscription_data)` sends up to 1000 changes
in one Zulip request. Each item contains `stream_id`, `property` and `value`.
Input value kinds are checked before calling the Executor. With channel policy
active, the Executor checks every distinct target against visibility and write
rules before sending the bulk request; denial or metadata lookup failure prevents
that request entirely. Metadata preflight is bounded by a 30-second timeout.

This preflight is not a transaction or snapshot of Zulip permissions. The server
still authorizes the final write. The MCP does not retry or promise rollback if
Zulip rejects part of a request or the response is lost. Read-only mode hides this
tool and denies direct execution before any upstream access.

## Removing subscriptions

`zulip_update_subscriptions(add=None, delete=None)` combines additions and
removals for the caller in one PATCH, with at least one change and at most
1000 entries total. Addition records accept name, description and color;
deletions are names. The pinned server wraps these changes in one transaction.
With active channel policy, every existing target is checked before writing;
the same post-write name/ID checks and uncertainty rules as subscribe/unsubscribe
apply. Unresolved addition targets follow the creation rules below. Read-only
mode hides the tool and denies generic execution before any lookup. A lost
response is reported as uncertain and never automatically retried.

Subscription item `color` is supported through a source-backed schema overlay:
the pinned server accepts it although its OpenAPI file omits the property.
Other unknown item properties remain forbidden.

`zulip_subscribe(subscriptions, principals=None, options=None)` exposes the
catalog's subscribe operation, including channel descriptions and creation
settings, notification options and permission groups. Principals may be user
IDs or API email addresses; omission targets the caller. Options cannot replace
either target argument, and the catalog rejects unsupported fields. Read-only
mode hides the tool. Results retain per-user successes/errors and uncertainty.

With channel policy enabled, a 400/BAD_REQUEST name lookup may proceed to
subscribe/create only if the requested name is write-allowed as both a public
and a private channel. This does not assert that a failed lookup proves absence:
the name could refer to an inaccessible existing channel, which Zulip must still
authorize. Other lookup errors do not trigger a write. Policies depending on the
unknown channel's privacy continue to deny it. After writing, the actual channel
is resolved and its write permissions rechecked; failed verification reports an
uncertain write and withholds data. Existing resolved targets retain ID-change
checks. Without channel policy, Zulip applies normal creation permissions.
Tests use mock HTTP, not a real Zulip server.

`zulip_unsubscribe(subscriptions, principals=None)` removes subscriptions by
channel name. Omit `principals` to act on the caller; otherwise supply either a
list of positive user IDs or a list of Zulip API email addresses. The server
checks whether the caller can change those users' memberships. No credentials
or acting identity are switched.

The shared Executor preflights channel visibility/write permissions and preserves
all request parameters. When channel policy is active, it checks the resolved
channel IDs again after writing. Zulip's name-based API cannot make concurrent
renames atomic. A changed name or lost access during the postcheck reports
`subscription_target_changed` with `uncertain_write`; this includes cases where
unsubscribing the caller removed their access. Do not retry automatically.
Read-only mode hides the tool and denies the catalog operation.

The preflight and postflight each have a 30-second deadline across all targets.
A preflight timeout prevents the write. A postflight timeout reports
`channel_policy_lookup_timeout` with `uncertain_write=true` and withholds data;
the mutation may already have succeeded. Neither timeout retries the mutation.

`zulip_get_subscription_status(user_id, stream_id)` checks a user's membership
using the caller's permissions, including bot users on the pinned Zulip version.
It is available in read-only mode. A returned `is_subscribed=false` means no
subscription; visibility/permission errors remain errors and are not converted
to false. Channel visibility rules apply before the membership request.

`zulip_get_subscriptions(include_subscribers="false")` reads the caller's current
subscriptions, preserving notification, color, mute and other settings. The
subscriber selector follows the pinned API exactly: strings `"false"`, `"true"`
and `"partial"`. Partial mode can return `partial_subscribers` for large channels;
it must not be interpreted as a complete membership list. Existing channel
policy filters channels without stripping allowed subscription fields.

## Archiving and topic deletion

`zulip_update_channel_message_flags(stream_id, flag, op, topic=None, ...)` uses
the modern `update-message-flags-for-narrow` endpoint. Set `flag="read"` with
`op="add"` to mark read, or `op="remove"` to mark unread; other personal flags
are validated by Zulip. The channel is bound by ID, and an optional empty topic
selects the unnamed topic. Anchor, counts before/after it, and anchor inclusion
are explicit inputs. Defaults match the reference's first-unread/1000-after
range. All upstream counts, first/last processed IDs and found-oldest/newest
flags are retained. The server can process less than requested; the tool neither
claims entire-channel completion nor retries or continues automatically.

With channel policy active, generic narrow flag updates currently require one
positive, non-negated numeric channel/stream term; the target is checked against
visibility and write rules. Broader or name-only narrows are denied pending their
own scoped adapter. Without channel policy, the catalog accepts the full API
narrow schema. Read-only mode hides the tool and rejects the underlying operation
before metadata access. Current evidence is mock HTTP and policy tests.

`zulip_mark_channel_read(stream_id, topic=None)` marks the caller's messages
read in a whole channel, or in exactly one topic when `topic` is supplied.
An empty string selects the unnamed topic. The operation uses the legacy
`mark-stream-as-read` / `mark-topic-as-read` endpoints present in the pinned
server. Zulip deprecates these endpoints in favor of
`update-message-flags-for-narrow`, which the unfoldingWord reference already
uses; broader modern flag workflows remain tracked separately. Both legacy
operations now enforce shared channel
visibility/write rules and read-only denial, including generic executor calls.
Writes are not retried automatically. Evidence is mocked HTTP, not live Zulip.

`zulip_set_default_channel(stream_id, enabled)` adds or removes an organization
default channel for new users. Zulip requires administrator permission and
rejects unsuitable channels (for example private channels when adding defaults).
The MCP checks channel write policy without changing caller identity, and hides
the tool in read-only mode. Existing users' subscriptions are not changed by
this setting. Both POST and DELETE forms are covered with mock HTTP.

`zulip_set_topic_visibility(stream_id, topic, visibility_policy)` changes the
authenticated caller's topic preference: `0` resets it, `1` mutes, `2` unmutes
even in a muted channel, and `3` follows. Empty topic names are supported.
The shared Executor checks channel visibility and write policy for this API
operation, including generic execution. Read-only mode hides the tool and denies
the operation before metadata lookup. This is a personal setting, not a change
to other users' preferences. No automatic retry is performed.

`zulip_archive_channel(stream_id)` archives the selected channel.
`zulip_delete_topic(stream_id, topic_name)` deletes its topic messages; the topic
name may be empty. Both are marked destructive, use the acting caller's API
permissions and channel write policy, and are hidden/denied in read-only mode.

Topic deletion can succeed only partially. Inspect the returned `complete` flag;
`false` means messages remain and an explicit subsequent request is needed. The
MCP does not automatically repeat deletion, including after an uncertain response.
Archiving is distinct from deleting a topic's messages. Current tests are mocked
HTTP and policy checks; no real channel was archived or topic deleted.

The catalog `update-stream` operation now checks both the current channel and
its requested resulting name/private status against channel write policy. This
prevents renaming a channel out of the permitted area or renaming a currently
write-denied channel into an allowed name to bypass its current restriction.
`zulip_update_channel(stream_id, settings)` exposes the complete catalog request
schema without duplicating its parameters. Use `zulip_describe_operation` for
`update-stream` to inspect supported fields. The settings object cannot replace
the explicit channel ID; unknown fields are rejected by the Executor. Nullable
folder settings are preserved and covered by a wire test. Parameter wire tests
cover every catalog creation/update field; live behavior remains unverified.


## Independent review follow-through

Channel reads discard all accumulated output if any Principal field changes,
including rebinding the same local identity to a different email or API key.
Under channel policy, flag narrows containing `with` are rejected in both object
and legacy list forms because Zulip replaces their channel/topic scope. Rename
policy checks use the same whitespace stripping as the pinned server.

Remaining schema gap: explicit `null` for `subscribe.subscriptions[].color` is
not accepted by the overlay, although the pinned server accepts it. Omit `color`
to use its default. This minor gap remains part of the complete API milestone.
The similar identity-binding pattern in existing search helpers also remains a
separate follow-up; the channel fix does not certify those helpers.

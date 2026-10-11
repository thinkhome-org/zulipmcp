# Message search

`zulip_search_messages` reads messages with the authenticated caller's executor
and channel policy. It accepts a text query, channel name/ID, topic (including an
empty topic), sender name/email/ID, or additional Zulip narrow terms. Sender
names are resolved from the caller-visible directory; ambiguous candidates are
returned without guessing or fetching messages for an arbitrary match.

Dates use ISO syntax and normalize to UTC; naive dates mean UTC. Explicit
`after_time` and `before_time` are inclusive. Relative `last_hours`, `last_days`
and `hours_back` windows end at `as_of` or the current time. Multiple constraints
intersect; reversed or empty intervals fail before network requests.

The first page can use the actual Zulip `anchor_date` parameter. Timestamps are
also filtered locally. Subsequent pages use message IDs, including the original
traversal cursor when channel policy hides a whole page. `limit` bounds returned
messages (1–1000); `max_pages` bounds requests (1–50). A page's raw Markdown is
preserved. Invalid timestamps, history flags or history-page structure return an
error rather than a partial successful result. Caller revocation or identity
change before/after a fetch returns no message data.

To continue, reuse `next_cursor`, narrow, sender, sort and returned absolute
`after_time`/`before_time`. Omit the original relative intervals so the window
does not move between calls. `has_more` means upstream traversal remains; it
cannot promise further matching messages. `history_limited` signals upstream
history restrictions, even when traversal says exhausted. No snapshot isolation
is claimed: messages can be edited/deleted and permissions can change between
calls. Results sort by timestamp and ID within the retrieved sample.

Sorting supports newest/oldest and relevance within the retrieved sample.
Relevance counts case-folded whole-word occurrences of positive search terms,
with topic occurrences weighted twice; timestamp and ID break ties. It requires
positive text terms, returns scores and `ranking_scope=retrieved_sample`, and
retains the underlying chronological cursor. It does not claim server stemming,
semantic relevance or a globally best result outside the bounded sample.
Full reference parity remains in progress; bounded multi-channel history is
described below.
Combined search and message-context/link helpers are described below. Current evidence uses unit/SDK/stdio and simulated upstream
fixtures; live Zulip behavior is not yet established.

## Single-message context

`zulip_message_context` retrieves an authorized message as raw Markdown, preserving
empty topics. It returns the message, its channel/topic or DM type, a stable
fixed-realm URL, an escaped Markdown link, and a JSON source record. The source
record separates content from API metadata; it is not a digital signature and
message content cannot grant permission. Owner changes after fetching suppress
all returned data. Optional `before`/`after` (0–100 each) fetch two bounded pages in the same
conversation and return raw/formatted messages, before/after traversal cursors,
exhaustion flags and history restrictions. A zero side is not queried; its
exhaustion flag is null. Additional traversal can use the history tool with the
same channel/topic or exact DM recipient narrow and returned cursor.

`zulip_construct_narrow` is a local, read-only helper: it combines existing terms
with channel/topic/sender/text, has-attachment/link/image flags, message-state
flags, message/near IDs and direct-message recipient filters. False flags generate
negated terms; omitted flags add nothing. It never resolves names or calls Zulip.
Pass its returned `result` array as the search/history `narrow` argument.

## Checking a narrow

`zulip_check_matches` checks up to 1000 supplied message IDs against a narrow,
removing duplicate IDs before the API call. Returned IDs must be a subset of the
request. The result includes matching IDs, snippets, and counts over distinct
requested IDs. With a channel policy, the executor checks each matching message
through its existing guarded message reader, under a 30-second total policy
lookup budget. Hidden matches are omitted and count as non-matching. Malformed
responses or lookup failures return no partial snippets. The wrapper checks the
caller again after the awaited request. Snippets are untrusted rendered HTML.

Set `include_summary=true` to receive counts by sender/channel and grouped topics
for the returned sample. IDs remain distinct; topic grouping is case-insensitive
within one channel. Direct messages are counted separately and never treated as
channels. The summary carries `scope=returned_sample`; it is not a count over all
matching realm history and inherits the result's limits and access restrictions.

## Topic-name search

`zulip_search_topics(query, stream_ids, limit=100, cursor=None)` searches topic
names case-insensitively in up to 20 explicitly selected channels. Empty topic
names are supported. Results include channel IDs and each topic's latest message
ID. Reuse `next_cursor` with the same query and ordered channel IDs. Lists are
read live, so changes between calls can shift the offset; this is not a snapshot.

Inspect `partial` and `errors` even when `exhausted` is true. Failed channels
require an explicit retry. Revoking or changing the caller during traversal
returns an authentication error without publishing results from prior channels.

## Combined search

`zulip_advanced_search(query, sections=["messages"], limit=100, cursors=None,
stream_ids=None)` supports `messages`, `users`, `streams`, and `topics` in one
caller-bound operation. The limit applies independently to each section. Results
retain each section's `data` and `error`; inspect the top-level `partial` flag.
An authentication failure discards all accumulated results.

For messages, pass `next_cursor` back as `cursors.messages`. For users/streams,
use `next_offset` as `cursors.users` / `cursors.streams`. For topics, pass the
whole `next_cursor` as `cursors.topics`. Keep the query, selected sections and
explicit channel IDs unchanged. With no explicit IDs, topic search discovers
20 caller-visible channels at a time, returning a continuation across directory
batches as needed. The query matches topic names independently of channel names.
Directory/topic continuations use live offsets; concurrent changes can shift
positions. Message summaries describe only returned messages. For message date,
sender, narrow and ranking controls use `zulip_search_messages` directly.

## Multi-channel history

`zulip_multi_channel_history(channels=None, hours_back=24, as_of=None, sender=None,
per_channel_limit=100, max_pages=10, cursors=None)` merges history in ascending
message-ID order, removing duplicate IDs. Omitted channels select Zulip's public
channel narrow. An explicit empty list returns no messages. Explicit channels
accept up to 20 names or IDs, including private channels the caller can read.

The limit is **per channel**, at most 200 messages each; every fetched matching
message is retained in the merged output. There is no additional global slice
that could silently skip messages on continuation. `max_pages` bounds traversal
per channel. All channels use the same inclusive time window, calculated once.

Continue using returned `as_of`, `hours_back` and `cursors`, preserving the ordered
channel list and sender. Cursor keys are zero-based channel indexes as strings;
null means that channel is complete and will be skipped. Failed channels keep
their incoming cursor (or no entry if they have not started). Inspect `partial`,
`errors` and `history_limited`; exhaustion cannot establish unrestricted history.
Revocation or a changed caller discards the whole call's results. Continuations
are not snapshots: edits, deletions and access changes remain possible.

### Formatting and participant context

History accepts `include_formatted=True` and an IANA `timezone` (default UTC).
It reuses the shared XML formatter, including escaped content and reaction
summaries. Raw `messages` remain present. Formatter conventions such as `/nb`
omission affect formatted text only.

`participant_id` selects context windows around that sender, independently in
each channel/topic. The result appears in `context_messages` and, when requested,
`formatted`; raw messages and traversal cursors are unchanged. Context is bounded
by the fetched sample (`context_scope=retrieved_sample_per_topic`), so a page edge
can cut off neighboring messages. Retrieve further pages for more context.
The existing context-window rules include three nearby messages and additional
time-bounded neighbors. Do not combine `participant_id` with `sender`: filtering
upstream to only that sender would remove the neighbors needed for context.
Invalid timezone or presentation options fail before upstream requests.

Search also accepts `include_formatted=True` and `timezone` (IANA name, default
UTC). This returns the shared escaped XML representation while preserving raw
messages. Formatted messages are displayed in ascending message-ID order;
raw results retain the requested newest/oldest/relevance ordering. To retrieve
and format a topic, supply `channel` and `topic` (including an empty topic) without
a text query. Formatting does not change filters, result counts or cursors.

Search, match checks, directory/topic discovery, advanced search and aggregate
history compare the complete immutable Principal between dependent requests.
Changing credentials or email on the same local identity discards accumulated
results with `authentication_required`; no previous caller data is returned.
Regression: `tests/unit/test_search_rebinding.py` (12 cases, observed failing
before the fix). Mock tests verify local isolation, not live Zulip behavior.

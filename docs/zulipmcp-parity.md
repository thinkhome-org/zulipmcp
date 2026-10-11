# ZulipMCP feature parity requirement

User-required minimum: all functionality of `windborne/zulipmcp` except Hermes. Reference audited on 2026-10-11 at commit `10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75` (Apache-2.0). This extends the original API plan; endpoint coverage alone cannot satisfy it.

See also the required [additional reference inventory](additional-mcp-parity.md) for unfoldingWord and akougkas. This document covers windborne only; all three are additive to full API scope.

## MCP tools

The pinned source registers **27 tools**. Names below identify reference behavior, not an assertion that ThinkHome currently implements the same interface. All require explicit parity tests; generic API operations are only supporting primitives.

| Reference tool | Source | ThinkHome parity |
|---|---|---|
| `set_context` | [definition](https://github.com/windborne/zulipmcp/blob/10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75/zulipmcp/mcp.py#L363) | Pending dedicated wrapper and parity tests |
| `reply` | [definition](https://github.com/windborne/zulipmcp/blob/10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75/zulipmcp/mcp.py#L400) | Pending dedicated wrapper and parity tests |
| `listen` | [definition](https://github.com/windborne/zulipmcp/blob/10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75/zulipmcp/mcp.py#L504) | Pending dedicated wrapper and parity tests |
| `end_session` | [definition](https://github.com/windborne/zulipmcp/blob/10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75/zulipmcp/mcp.py#L710) | Pending dedicated wrapper and parity tests |
| `list_streams` | [definition](https://github.com/windborne/zulipmcp/blob/10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75/zulipmcp/mcp.py#L795) | Pending dedicated wrapper and parity tests |
| `get_stream_topics` | [definition](https://github.com/windborne/zulipmcp/blob/10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75/zulipmcp/mcp.py#L824) | Pending dedicated wrapper and parity tests |
| `get_stream_members` | [definition](https://github.com/windborne/zulipmcp/blob/10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75/zulipmcp/mcp.py#L847) | Pending dedicated wrapper and parity tests |
| `get_messages` | [definition](https://github.com/windborne/zulipmcp/blob/10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75/zulipmcp/mcp.py#L869) | Pending dedicated wrapper and parity tests |
| `get_message_by_id` | [definition](https://github.com/windborne/zulipmcp/blob/10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75/zulipmcp/mcp.py#L921) | Partial: caller-bound message/reaction tools and wire tests; reference formatting and configured policies pending |
| `get_message_link` | [definition](https://github.com/windborne/zulipmcp/blob/10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75/zulipmcp/mcp.py#L939) | Pending dedicated wrapper and parity tests |
| `verify_message` | [definition](https://github.com/windborne/zulipmcp/blob/10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75/zulipmcp/mcp.py#L966) | Pending dedicated wrapper and parity tests |
| `send_message` | [definition](https://github.com/windborne/zulipmcp/blob/10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75/zulipmcp/mcp.py#L1007) | Partial: caller-bound message/reaction tools and wire tests; reference formatting and configured policies pending |
| `send_direct_message` | [definition](https://github.com/windborne/zulipmcp/blob/10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75/zulipmcp/mcp.py#L1029) | Partial: caller-bound message/reaction tools and wire tests; reference formatting and configured policies pending |
| `add_reaction` | [definition](https://github.com/windborne/zulipmcp/blob/10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75/zulipmcp/mcp.py#L1050) | Partial: caller-bound message/reaction tools and wire tests; reference formatting and configured policies pending |
| `remove_reaction` | [definition](https://github.com/windborne/zulipmcp/blob/10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75/zulipmcp/mcp.py#L1064) | Partial: caller-bound message/reaction tools and wire tests; reference formatting and configured policies pending |
| `edit_message` | [definition](https://github.com/windborne/zulipmcp/blob/10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75/zulipmcp/mcp.py#L1083) | Partial: dedicated editing/move/resolve wrappers and wire tests; normalization and configured access policies pending |
| `move_messages` | [definition](https://github.com/windborne/zulipmcp/blob/10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75/zulipmcp/mcp.py#L1106) | Partial: dedicated editing/move/resolve wrappers and wire tests; normalization and configured access policies pending |
| `resolve_topic` | [definition](https://github.com/windborne/zulipmcp/blob/10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75/zulipmcp/mcp.py#L1151) | Partial: dedicated editing/move/resolve wrappers and wire tests; normalization and configured access policies pending |
| `list_emoji` | [definition](https://github.com/windborne/zulipmcp/blob/10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75/zulipmcp/mcp.py#L1185) | Pending dedicated wrapper and parity tests |
| `typing` | [definition](https://github.com/windborne/zulipmcp/blob/10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75/zulipmcp/mcp.py#L1204) | Pending dedicated wrapper and parity tests |
| `stop_typing` | [definition](https://github.com/windborne/zulipmcp/blob/10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75/zulipmcp/mcp.py#L1223) | Pending dedicated wrapper and parity tests |
| `get_user_info` | [definition](https://github.com/windborne/zulipmcp/blob/10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75/zulipmcp/mcp.py#L1246) | Pending dedicated wrapper and parity tests |
| `resolve_name` | [definition](https://github.com/windborne/zulipmcp/blob/10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75/zulipmcp/mcp.py#L1286) | Pending dedicated wrapper and parity tests |
| `get_subscribed_streams` | [definition](https://github.com/windborne/zulipmcp/blob/10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75/zulipmcp/mcp.py#L1307) | Pending dedicated wrapper and parity tests |
| `fetch_image` | [definition](https://github.com/windborne/zulipmcp/blob/10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75/zulipmcp/mcp.py#L1324) | Partial: staged upload/download; local export, image viewing and embedding convenience still pending |
| `fetch_file` | [definition](https://github.com/windborne/zulipmcp/blob/10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75/zulipmcp/mcp.py#L1338) | Partial: staged upload/download; local export, image viewing and embedding convenience still pending |
| `upload_file` | [definition](https://github.com/windborne/zulipmcp/blob/10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75/zulipmcp/mcp.py#L1353) | Partial: staged upload/download; local export, image viewing and embedding convenience still pending |

## Python library surface

`__init__.py` explicitly exports **45 names**. Each callable needs a caller-bound equivalent and documented usage; no process-global credential/session state may be shared across HTTP users.

`configure`, `mcp`, `normalize_zulip_markdown`, `get_client`, `get_user_email`, `resolve_name`, `is_stream_private`, `get_messages`, `format_messages`, `format_topics`, `get_messages_formatted`, `get_profile`, `list_streams`, `get_stream_topics`, `get_topic_messages`, `fetch_new_messages`, `send_message`, `add_reaction`, `remove_reaction`, `edit_message`, `send_direct_message`, `send_typing`, `list_emoji`, `get_emoji_count`, `summarize_reactions`, `check_reactions_on`, `discover_message_context`, `get_custom_profile_fields`, `get_user_info`, `get_message_by_id`, `verify_message`, `get_message_link`, `get_subscribed_streams`, `get_stream_members`, `download_file`, `save_file`, `save_image`, `ensure_subscribed`, `register_event_queue`, `get_events`, `delete_event_queue`, `set_dismiss_emoji`, `get_dismiss_emoji`, `is_dismiss_reaction`, `check_dismissed`.

Additional public core helpers not re-exported by `__init__.py`: `is_private_stream_allowed`, `is_stream_write_allowed`, `set_ignored_streams`, `get_ignored_streams`, `filter_for_bot`, `move_messages`, `get_emoji_info`, `upload_file`, `is_bot_subscribed`. Preserve the `sign_off` compatibility alias and `run_server` entry point as well.

## Behavioral requirements beyond tool names

| Area | Required behavior | Current state |
|---|---|---|
| Session | Context/history initialization, trigger anchor, requester identity, automatic environment context, reply catches up before sending, dismiss-before-reply, farewell/duration, automatic typing start after context/reply, configurable suppression with reply-time stop, unconditional idle/exit stop, hooks and exit markers | Pending |
| Listening | Narrowed real-time queue, catch-up, auto-subscribe when permitted, renewal after expiration with explicit history-gap status, reactions including custom robot_ear, cancellation/dismissal, keepalive/progress, external interrupt file, renewable long waits | Queue foundation implemented; reference listen behavior pending |
| Message access | Paginated time/channel/sender/as-of history; before-ID and message-ID context; deduplication; name/email lookup; message links; rendered-message verification; topic resolution and moves | Generic primitives only; high-level parity pending |
| Formatting | XML-style message/topic formatting, combined consecutive messages, context windows, timestamp/timezone display, reaction summaries, selectable stream fields, safe escaping of untrusted content | Pending |
| Policies | /nobots and /nb filtering, ignored streams, configurable private/read and write allowlists, custom dismiss reactions, maximum message length including prefixes | Pending |
| Markdown | Blank lines before tables and bold/link normalization, preserving fenced/indented/inline code; explicit disable setting | Pending |
| Hooks/config | message_prefix, on_session_end, on_set_context, on_reply, dismiss_emoji, interrupt_file, auto_typing_start; .zuliprc/ZULIP_RC_PATH, timezone/cache/log settings | Pending |
| Transports | stdio and authenticated legacy SSE in addition to the existing Streamable HTTP transport | stdio/HTTP implemented; SSE pending |
| Direct launcher | Claude and Codex, one foreground session, custom binary/workdir/prompt/config, trigger/requester context, renewable timeout, passthrough flags, dry-run, explicit permission modes | Pending |
| Mention listener | Claude, Codex and OpenCode; one live session per stream/topic, mention dispatch, public subscription, private membership checks, eyes reaction, lifecycle/log management | Pending |
| Backend adapters | Claude strict config; Codex command/HTTP MCP translation, top-level flags, env/header secret handling, permission presets; OpenCode local/remote config, instructions/model/agent and long-listen timeout | Pending |
| Files | Fetch image/file for client use, controlled local export, upload and Markdown embedding; HTTP uses owned handles, local paths only under configured roots | Backend transfer implemented; compatibility wrappers/export pending |

Hermes gateway/plugin behavior is explicitly excluded by the user. No other feature is excluded solely because it is absent from the original plan.

## Implementation and acceptance

- Implement the above on the existing caller-scoped executor/store; never import reference process-global auth/session state into multi-user HTTP.
- Retain bounded 25-second event polls. Add renewable, cancellable long-listen orchestration for reference parity; distinguish poll duration from total listen duration.
- Agent launching is an opt-in operator CLI capability, never an arbitrary shell MCP tool. Preserve supported permission choices; require explicit operator configuration for unrestricted modes.
- File compatibility preserves the existing allowed-root boundary and owner-bound HTTP handles. A different transport representation is documented and tested; it must still let the client actually view/save the content.
- Before release, recheck upstream main and review any changes since the pinned reference.
- Add a parity manifest/test matrix with evidence for every tool, exported library capability, listener/backend and behavioral row before claiming parity. Include upstream regression scenarios (normalization, timezones, interrupt handling, direct launch, automatic typing).
- Do not count mocked backend command construction as an actual authenticated agent-provider run; publish any external-provider verification limits.
- Preserve third-party notices if upstream code is reused. This source inventory itself does not relicense upstream code.

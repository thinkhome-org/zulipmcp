# Channel API parameter audit

Source: pinned Zulip `b31982251ff82d0b02e59f63ad21f19204e37aea`.
This is a schema inventory for the current channel milestone, not endpoint certification.
Parameter names below are extracted from the bundled schema including overlays.

| Operation | HTTP route | Accepted top-level parameters |
|---|---|---|
| `get-streams` | `GET /streams` | `include_public`, `include_web_public`, `include_subscribed`, `exclude_archived`, `include_all_active`, `include_all`, `include_default`, `include_owner_subscribed`, `include_can_access_content` |
| `get-stream-id` | `GET /get_stream_id` | `stream` |
| `get-stream-by-id` | `GET /streams/{stream_id}` | `stream_id` |
| `get-stream-topics` | `GET /users/me/{stream_id}/topics` | `stream_id`, `allow_empty_topic_name` |
| `get-subscribers` | `GET /streams/{stream_id}/members` | `stream_id` |
| `get-subscriptions` | `GET /users/me/subscriptions` | `include_subscribers` |
| `get-subscription-status` | `GET /users/{user_id}/subscriptions/{stream_id}` | `user_id`, `stream_id` |
| `subscribe` | `POST /users/me/subscriptions` | `subscriptions`, `principals`, `authorization_errors_fatal`, `announce`, `invite_only`, `is_web_public`, `is_default_stream`, `history_public_to_subscribers`, `message_retention_days`, `topics_policy`, `can_add_subscribers_group`, `can_remove_subscribers_group`, `can_administer_channel_group`, `can_delete_any_message_group`, `can_delete_own_message_group`, `can_move_messages_out_of_channel_group`, `can_move_messages_within_channel_group`, `can_send_message_group`, `can_subscribe_group`, `can_resolve_topics_group`, `can_create_topic_group`, `folder_id`, `send_new_subscription_messages` |
| `unsubscribe` | `DELETE /users/me/subscriptions` | `subscriptions`, `principals` |
| `update-subscriptions` | `PATCH /users/me/subscriptions` | `delete`, `add` |
| `update-subscription-property` | `PATCH /users/me/subscriptions/{stream_id}` | `stream_id`, `property`, `value` |
| `update-subscription-settings` | `POST /users/me/subscriptions/properties` | `subscription_data` |
| `update-stream` | `PATCH /streams/{stream_id}` | `stream_id`, `description`, `new_name`, `is_private`, `is_web_public`, `history_public_to_subscribers`, `is_default_stream`, `message_retention_days`, `is_archived`, `folder_id`, `topics_policy`, `can_add_subscribers_group`, `can_remove_subscribers_group`, `can_administer_channel_group`, `can_delete_any_message_group`, `can_delete_own_message_group`, `can_move_messages_out_of_channel_group`, `can_move_messages_within_channel_group`, `can_send_message_group`, `can_subscribe_group`, `can_resolve_topics_group`, `can_create_topic_group` |
| `archive-stream` | `DELETE /streams/{stream_id}` | `stream_id` |
| `delete-topic` | `POST /streams/{stream_id}/delete_topic` | `stream_id`, `topic_name` |
| `add-default-stream` | `POST /default_streams` | `stream_id` |
| `remove-default-stream` | `DELETE /default_streams` | `stream_id` |
| `update-user-topic` | `POST /user_topics` | `stream_id`, `topic`, `visibility_policy` |
| `mark-stream-as-read` | `POST /mark_stream_as_read` | `stream_id` |
| `mark-topic-as-read` | `POST /mark_topic_as_read` | `stream_id`, `topic_name` |
| `update-message-flags-for-narrow` | `POST /messages/flags/narrow` | `anchor`, `include_anchor`, `num_before`, `num_after`, `narrow`, `op`, `flag` |

## Remaining evidence and implementation

- Confirmed schema gap: `subscribe.subscriptions[].color` is accepted by
  `AddSubscriptionData` (`zerver/views/streams.py:554-565`) and consumed by
  `add_subscriptions_backend`, but absent from the pinned OpenAPI subscription
  item. A source-backed nested parameter overlay now adds `color` while keeping
  `additionalProperties: false`. The contract regression checks both support
  for color and rejection of unrelated fields.

- `subscribe` and `update-stream`: wire fixtures now enumerate every top-level field, boolean values, topic policies, retention variants, nullable update folder, and group IDs/member objects (including old/new updates). Cross-field server permission behavior still requires real Zulip testing.
- `update-subscriptions`: the dedicated adapter and existing-channel policy guard use one PATCH. The pinned server composes add then delete inside `transaction.atomic(savepoint=False)` (`zerver/views/streams.py`, `compose_views`); a lost HTTP response still leaves the outcome uncertain.
- Creation under channel policy: failed lookup does not prove absence. A 400/BAD_REQUEST lookup may proceed only for addition targets whose name is write-allowed at both privacy values; the resulting channel is verified after the write. Visibility-dependent policies still deny unresolved names. This preserves policy without treating an inaccessible private channel as a new public channel.
- Name-based subscription writes have separate 30-second preflight/postflight deadlines and explicit race/uncertain-write handling. They cannot acquire an atomic remote name lock.
- Modern narrow flag writes currently support policy-scoped numeric channel constraints; broader and name-only narrows under policy remain unsupported.
- Subscription status has a dedicated read-only tool and mocked allowed/denied-channel wire evidence; real user/bot permissions still need live coverage.
- Actual role/permission behavior requires an isolated real Zulip instance. Mock HTTP verifies serialization and local guards only.

## Test evidence map

- `tests/unit/test_channels.py`: discovery, topic ordering, optional section failures, caller changes, profile resolution, presentation fields.
- `tests/integration/test_channel_wire.py`: MCP calls through the real Executor with mock HTTP; selected request fields and partial-response preservation.
- `tests/integration/test_channel_parameters.py` and `tests/contract/test_channel_schema.py`: every creation/update field, nested color correction and strict unknown-field rejection.
- `tests/integration/test_subscription_batch.py`: combined PATCH, all-target preflight, read-only mode, empty/oversized input and uncertain writes without retry.
- `tests/unit/test_subscription_policy.py`: channel visibility/write policy and read-only behavior for supported mutations.
- `tests/integration/test_stdio.py` and `tests/unit/test_read_only.py`: tool inventory and read-only exposure.

A passing test suite does not close the unimplemented items above or the full-project parity plan.

Completed regression (2026-10-11): `uv run pytest` — 786 passed in 871.57s.
Ruff check/format, mypy (`src scripts`), catalog check and wheel/sdist build passed.
Evidence verifies local contracts; real Zulip roles and private channels remain pending.

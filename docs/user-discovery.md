# User discovery

These tools use the caller's fixed Zulip origin and credentials. They
work over stdio and authenticated HTTP, with read-only tools available in read-only mode. No directory
cache is shared between callers; each invocation asks Zulip for currently visible
data. The tools do not grant visibility of hidden email addresses or profiles.

| Tool | Behavior |
|---|---|
| `zulip_get_users` | List accessible users, optionally filtering `user_ids`; optional custom profile values and Gravatar handling. ID filtering uses Zulip 12.3's native parameter. |
| `zulip_get_user` | Exactly one `user_id` or `email`. With `include_custom_profile_fields=true`, fetch values and realm field definitions (`custom_fields`, including names/types) together. |
| `zulip_get_own_user` | Return the authenticated caller's profile. |
| `zulip_resolve_user` | Find candidates by name or visible email, returning stable user IDs, names, API emails and match scores. |
| `zulip_get_presence` | Presence for one `user_id_or_email`, or the organization when omitted. |
| `zulip_get_user_status` | Read an accessible user's status text and emoji. |
| `zulip_get_user_groups` | List groups, optionally including deactivated groups. |
| `zulip_get_user_group_members` | List direct or transitive group members. |
| `zulip_is_user_group_member` | Check a user's direct or transitive membership. |
| `zulip_update_status` | Set the caller's own status text, emoji or legacy away flag. Empty text/emoji clears it; omitted values remain unchanged. |
| `zulip_manage_user_mute` | Mute/unmute another user for the caller's own notifications. |

The two write tools are hidden in read-only mode; their underlying API operations
are also blocked through generic execution and batches. They never accept an
acting user ID or credentials. Status text is limited to 60 characters; an empty
update is rejected. Writes are not retried, including on server errors.
Group and presence permissions, unavailable presence data, bot restrictions, and
already-muted/not-muted errors remain Zulip's decisions.

`zulip_get_user` preserves the original user response when field definitions are
unavailable, but returns an explicit `profile_fields_unavailable` error. It checks
current authentication again before the second request; revocation stops that
request. It never falls back to an administrator or another caller.

## Name matching

The default `fuzzy` mode first checks case-insensitive exact full names, first
names, API emails and delivery emails already visible to this caller. If there
are no exact candidates, it tries name substrings, then `difflib.SequenceMatcher`
on full and first names. The default similarity threshold is 0.6; callers may
choose a threshold from 0 to 1. Similarity is not a probability of identity.

`mode="substring"` implements windborne-style case-insensitive name substring
lookup without fuzzy fallback or email matching. Bots and deactivated users are
excluded by default; `include_bots` and `include_inactive` explicitly include them.
Results sort by descending score, then case-folded name and numeric ID.

The response has `status` (`resolved`, `ambiguous`, `not_found`), `matches`,
`total`, and `truncated`. A response with several candidates stays `ambiguous`
even if `limit=1` displays only one. Never automatically send to an ambiguous
candidate. The limit defaults to 20, maximum 100. Queries must contain nonblank
text and be at most 256 characters. Invalid or duplicate upstream user IDs and
malformed names/emails/boolean flags reject the entire resolution result.

## Channel policy and evidence

Fourteen underlying operations are classified as independent of channel content:
`get-users`, `get-user`, `get-user-by-email`, `get-own-user`,
`get-custom-profile-fields`, `get-presence`, `get-user-presence`, `get-user-status`,
`get-user-groups`, `get-user-group-members`, `get-is-user-group-member`,
`update-status`, `mute-user`, and `unmute-user`. They remain available with a
channel policy enabled; the last three are still denied in read-only mode. This policy
does not redact free-form user profiles or their links. `get-own-user` includes
`max_message_id`, which can reveal message-existence metadata, as do filtered
history cursors. User visibility and real-email visibility remain Zulip's checks.

Audited pinned Zulip `b31982251ff82d0b02e59f63ad21f19204e37aea`:
`zerver/views/users.py` (`get_user_data`, `get_member_backend`,
`get_members_backend`, `get_user_by_email`, `get_profile_backend`),
`zerver/lib/users.py` (`APIUserDict`, `format_user_row`, `get_users_for_api`), and
`zerver/views/custom_profile_fields.py:list_realm_custom_profile_fields`.
Additional source checks: `zerver/views/presence.py` (presence/status readers and
`update_user_status_backend`), `zerver/views/user_groups.py` (group/membership
readers), and `zerver/views/muted_users.py` (`mute_user`, `unmute_user`). These
return user/profile/presence/group data or update the acting caller's status and
mute relationships, not channel membership or message bodies.

Original implementation based on behavior inspected in pinned reference sources:
windborne `core.resolve_name` and `mcp.get_user_info`; akougkas
`tools/users.py` and `core/cache.py:UserCache.resolve_user`. The references' full
cache/configuration and complete discovery parity remain pending. Tests cover matching, malformed data, wire parameters, stdio
inventory, read-only discovery, and authenticated two-user HTTP isolation with
mid-operation token revocation, status clearing, group membership parameters,
read-only write denials and no retries of uncertain user writes. A real Zulip integration is still required.

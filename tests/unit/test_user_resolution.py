import pytest

from thinkhome_zulip_mcp.users import resolve_users

USERS = [
    {
        "user_id": 1,
        "full_name": "Alice Smith",
        "email": "user1@example",
        "delivery_email": "alice@example",
        "is_active": True,
        "is_bot": False,
    },
    {
        "user_id": 2,
        "full_name": "Alice Jones",
        "email": "user2@example",
        "is_active": True,
        "is_bot": False,
    },
    {
        "user_id": 3,
        "full_name": "Build Bot",
        "email": "bot@example",
        "is_active": True,
        "is_bot": True,
    },
    {
        "user_id": 4,
        "full_name": "Former Member",
        "email": "old@example",
        "is_active": False,
        "is_bot": False,
    },
]


@pytest.mark.parametrize("query", ["ALICE SMITH", " alice@example ", "user1@example"])
def test_exact_name_or_visible_email_resolves_stable_id(query):
    result = resolve_users(USERS, query)
    assert result["status"] == "resolved"
    assert result["matches"][0]["user_id"] == 1
    assert result["matches"][0]["confidence"] == 1


def test_ambiguous_first_name_is_not_silently_resolved_even_with_limit_one():
    result = resolve_users(USERS, "alice", limit=1)
    assert result["status"] == "ambiguous"
    assert result["total"] == 2
    assert result["truncated"] is True


def test_fuzzy_and_name_substring_behaviors():
    result = resolve_users(USERS, "Alic Smth", threshold=0.8)
    assert result["status"] == "resolved"
    assert result["matches"][0]["user_id"] == 1
    assert 0.8 <= result["matches"][0]["confidence"] < 1
    assert resolve_users(USERS, "SMI", mode="substring")["matches"][0]["user_id"] == 1
    assert (
        resolve_users(USERS, "alice@example", mode="substring")["status"] == "not_found"
    )


def test_bot_and_inactive_filters_are_explicit():
    assert resolve_users(USERS, "Build Bot")["status"] == "not_found"
    assert resolve_users(USERS, "Former Member")["status"] == "not_found"
    assert resolve_users(USERS, "Build Bot", include_bots=True)["status"] == "resolved"
    assert (
        resolve_users(USERS, "Former Member", include_inactive=True)["status"]
        == "resolved"
    )


@pytest.mark.parametrize(
    "changes",
    [{"user_id": True}, {"full_name": None}, {"is_bot": "false"}, {"email": None}],
)
def test_malformed_users_fail_without_partial_results(changes):
    with pytest.raises(ValueError, match="user response"):
        resolve_users([USERS[0], USERS[1] | changes], "Alice")


def test_duplicate_user_ids_rejected():
    with pytest.raises(ValueError, match="user response"):
        resolve_users([USERS[0], USERS[0]], "Alice")


@pytest.mark.parametrize(
    "options",
    [
        {"query": " "},
        {"threshold": float("nan")},
        {"threshold": 1.1},
        {"limit": 0},
        {"mode": "invalid"},
    ],
)
def test_invalid_resolution_parameters(options):
    with pytest.raises(ValueError, match="resolution request"):
        resolve_users(USERS, **({"query": "Alice"} | options))

import pytest

from thinkhome_zulip_mcp.channel_policy import ChannelPolicy


def test_disabled_policy_preserves_access():
    policy = ChannelPolicy.from_environment({})
    assert not policy.active
    assert policy.allows_channel("private", private=True, write=True)
    assert policy.allows_dm()


def test_jd_ranges_names_and_private_precedence():
    policy = ChannelPolicy.from_environment(
        {
            "ZULIPCHAT_CHANNEL_FILTER_ENABLED": "true",
            "ZULIPCHAT_JD_ALLOW_AREAS": "01,30-49",
            "ZULIPCHAT_JD_DENY_AREAS": "35-39",
            "ZULIPCHAT_CHANNEL_INCLUDE": "general,36 Special,blocked",
            "ZULIPCHAT_CHANNEL_EXCLUDE": "blocked",
        }
    )
    for name in ("01 Inbox", "30 Work", "42.01 Projects", "general", "36 Special"):
        assert policy.allows_channel(name, private=False)
    for name in ("blocked", "35 Work", "50 Outside", "ordinary", "300 Invalid"):
        assert not policy.allows_channel(name, private=False)
    assert not policy.allows_channel("general", private=True)
    assert not policy.allows_dm()


def test_explicit_windborne_allowlists_apply_to_reads_and_writes():
    policy = ChannelPolicy.from_environment(
        {
            "BOT_ALLOWED_PRIVATE_STREAMS": '["Secret", "Other"]',
            "BOT_ALLOWED_WRITE_STREAMS": "General, Secret",
        }
    )
    assert policy.active
    assert policy.allows_channel("secret", private=True, write=True)
    assert policy.allows_channel("Other", private=True)
    assert not policy.allows_channel("Other", private=True, write=True)
    assert not policy.allows_channel("Hidden", private=True)
    assert policy.allows_channel("Public", private=False)
    assert not policy.allows_channel("Public", private=False, write=True)


def test_empty_private_list_denies_private_and_all_token_allows():
    assert not ChannelPolicy.from_environment(
        {
            "BOT_ALLOWED_PRIVATE_STREAMS": "",
        }
    ).allows_channel("secret", private=True)
    assert ChannelPolicy.from_environment(
        {
            "BOT_ALLOWED_PRIVATE_STREAMS": '["__ALL__"]',
        }
    ).allows_channel("secret", private=True)


@pytest.mark.parametrize("value", ["-1", "100", "42-30", "x", "1-2-3"])
def test_invalid_area_ranges_fail_startup(value):
    with pytest.raises(ValueError):
        ChannelPolicy.from_environment({"ZULIPCHAT_JD_ALLOW_AREAS": value})


@pytest.mark.parametrize(
    "key,value",
    [
        ("ZULIPCHAT_CHANNEL_FILTER_ENABLED", "typo"),
        ("ZULIPCHAT_EXCLUDE_PRIVATE", "yes"),
        ("BOT_ALLOWED_PRIVATE_STREAMS", "[1]"),
        ("BOT_ALLOWED_WRITE_STREAMS", '["broken"'),
    ],
)
def test_invalid_policy_configuration_fails_startup(key, value):
    with pytest.raises(ValueError):
        ChannelPolicy.from_environment({key: value})


def test_filters_compose_without_bypassing_write_or_private_restrictions():
    policy = ChannelPolicy.from_environment(
        {
            "ZULIPCHAT_CHANNEL_FILTER_ENABLED": "1",
            "ZULIPCHAT_CHANNEL_INCLUDE": "special",
            "ZULIPCHAT_EXCLUDE_PRIVATE": "0",
            "ZULIPCHAT_EXCLUDE_DMS": "0",
            "BOT_ALLOWED_PRIVATE_STREAMS": "allowed",
            "BOT_ALLOWED_WRITE_STREAMS": "allowed",
        }
    )
    assert policy.allows_channel("special", private=False)
    assert not policy.allows_channel("special", private=True)
    assert not policy.allows_channel("special", private=False, write=True)
    assert policy.allows_dm()

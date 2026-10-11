import httpx
import pytest

from thinkhome_zulip_mcp.channel_policy import ChannelPolicy
from thinkhome_zulip_mcp.executor import Executor, Principal

CALLER = Principal("alice", "https://zulip.example", "alice", "synthetic")
POLICY = ChannelPolicy(jd_enabled=True)


@pytest.mark.asyncio
async def test_subscription_list_filters_channels_without_losing_subscription_fields():
    executor = Executor(
        channel_policy=POLICY,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "result": "success",
                    "subscriptions": [
                        {
                            "stream_id": 7,
                            "name": "30 Work",
                            "invite_only": False,
                            "color": "#fff",
                            "is_muted": True,
                        },
                        {"stream_id": 8, "name": "general", "invite_only": False},
                        {"stream_id": 9, "name": "40 Private", "invite_only": True},
                    ],
                },
            )
        ),
    )
    result = await executor.execute(CALLER, "get-subscriptions", {})
    assert result.error is None
    assert result.data["subscriptions"] == [
        {
            "stream_id": 7,
            "name": "30 Work",
            "invite_only": False,
            "color": "#fff",
            "is_muted": True,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup_error", [False, True])
async def test_other_user_channels_are_filtered_under_acting_identity(lookup_error):
    def upstream(request):
        assert (
            request.headers["authorization"]
            == httpx.BasicAuth("alice", "synthetic")._auth_header
        )
        if request.url.path.endswith("/users/22/channels"):
            return httpx.Response(
                200, json={"result": "success", "subscribed_channel_ids": [7, 8, 9]}
            )
        if lookup_error:
            return httpx.Response(503, json={"result": "error"})
        stream_id = int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(
            200,
            json={
                "result": "success",
                "stream": {
                    "stream_id": stream_id,
                    "name": "general" if stream_id == 8 else "30 Work",
                    "invite_only": stream_id == 9,
                },
            },
        )

    executor = Executor(channel_policy=POLICY, transport=httpx.MockTransport(upstream))
    result = await executor.execute(CALLER, "get-user-channels", {"user_id": 22})
    if lookup_error:
        assert result.error["code"] == "channel_policy_lookup_failed"
        assert result.data is None
    else:
        assert result.error is None
        assert result.data == {"result": "success", "subscribed_channel_ids": [7]}


@pytest.mark.asyncio
@pytest.mark.parametrize("ids", [[True], [7, 7], [-1], "invalid"])
async def test_invalid_channel_id_list_is_not_returned(ids):
    executor = Executor(
        channel_policy=POLICY,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"result": "success", "subscribed_channel_ids": ids}
            )
        ),
    )
    result = await executor.execute(CALLER, "get-user-channels", {"user_id": 22})
    assert result.error["code"] == "invalid_upstream_response"
    assert result.data is None


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [True, False])
async def test_subscribe_validates_all_existing_channels_before_write(allowed):
    calls = []

    def upstream(request):
        calls.append(request)
        if request.url.path.endswith("/get_stream_id"):
            return httpx.Response(200, json={"result": "success", "stream_id": 7})
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "stream": {
                        "stream_id": 7,
                        "name": "30 Work",
                        "invite_only": False,
                    },
                },
            )
        assert request.method == "POST"
        return httpx.Response(
            200, json={"result": "success", "subscribed": {"alice": ["30 Work"]}}
        )

    executor = Executor(
        channel_policy=ChannelPolicy.from_environment(
            {
                "BOT_ALLOWED_WRITE_STREAMS": "30 Work" if allowed else "Other",
            }
        ),
        transport=httpx.MockTransport(upstream),
    )
    result = await executor.execute(
        CALLER, "subscribe", {"subscriptions": [{"name": "30 Work"}]}
    )
    assert (result.error is None) == allowed
    assert sum(r.method == "POST" for r in calls) == int(allowed)
    if not allowed:
        assert result.error["code"] == "channel_policy_denied"


@pytest.mark.asyncio
async def test_subscribe_missing_channel_does_not_create_under_policy():
    def upstream(request):
        assert request.method == "GET"
        return httpx.Response(400, json={"result": "error", "msg": "Unknown channel"})

    result = await Executor(
        channel_policy=POLICY, transport=httpx.MockTransport(upstream)
    ).execute(CALLER, "subscribe", {"subscriptions": [{"name": "30 Missing"}]})
    assert result.error["code"] == "channel_policy_denied"


@pytest.mark.asyncio
async def test_subscribe_read_only_fails_before_lookup():
    def upstream(request):
        pytest.fail("read-only subscribe reached upstream")

    result = await Executor(
        read_only=True, channel_policy=POLICY, transport=httpx.MockTransport(upstream)
    ).execute(
        CALLER,
        "subscribe",
        {
            "subscriptions": [{"name": "30 Work"}],
        },
    )
    assert result.error["code"] == "read_only_policy"


@pytest.mark.asyncio
async def test_subscribe_reassigned_name_reports_uncertain_write_without_retry():
    writes = 0

    def upstream(request):
        nonlocal writes
        if request.method == "POST":
            writes += 1
            return httpx.Response(200, json={"result": "success"})
        channel_id = 8 if writes else 7
        if request.url.path.endswith("/get_stream_id"):
            return httpx.Response(
                200, json={"result": "success", "stream_id": channel_id}
            )
        return httpx.Response(
            200,
            json={
                "result": "success",
                "stream": {
                    "stream_id": channel_id,
                    "name": "30 Work",
                    "invite_only": False,
                },
            },
        )

    result = await Executor(
        channel_policy=POLICY, transport=httpx.MockTransport(upstream)
    ).execute(CALLER, "subscribe", {"subscriptions": [{"name": "30 Work"}]})
    assert result.error["code"] == "subscription_target_changed"
    assert result.uncertain_write
    assert result.data is None
    assert writes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "allowed,read_only", [(True, False), (False, False), (True, True)]
)
async def test_subscription_property_obeys_channel_write_policy(allowed, read_only):
    from urllib.parse import parse_qs

    calls = []

    def upstream(request):
        calls.append(request.method)
        if request.method == "GET":
            assert request.url.path == "/api/v1/streams/7"
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "stream": {
                        "stream_id": 7,
                        "name": "30 Work",
                        "invite_only": False,
                    },
                },
            )
        assert request.method == "PATCH"
        assert request.url.path == "/api/v1/users/me/subscriptions/7"
        assert parse_qs(request.content.decode()) == {
            "property": ["is_muted"],
            "value": ["true"],
        }
        return httpx.Response(200, json={"result": "success"})

    executor = Executor(
        channel_policy=ChannelPolicy(
            jd_enabled=True,
            write_allowlist=frozenset({"30 work"} if allowed else {"40 other"}),
        ),
        read_only=read_only,
        transport=httpx.MockTransport(upstream),
    )
    result = await executor.execute(
        CALLER,
        "update-subscription-property",
        {
            "stream_id": 7,
            "property": "is_muted",
            "value": True,
        },
    )
    if read_only:
        assert result.error["code"] == "read_only_policy"
        assert calls == []
    elif allowed:
        assert result.error is None
        assert calls == ["GET", "PATCH"]
    else:
        assert result.error["code"] == "channel_policy_denied"
        assert calls == ["GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize("second_allowed", [True, False])
async def test_bulk_subscription_settings_preflight_all_channels(second_allowed):
    import json
    from urllib.parse import parse_qs

    calls = []
    changes = [
        {"stream_id": sid, "property": "is_muted", "value": True} for sid in (7, 8)
    ]

    def upstream(request):
        calls.append(request.method)
        if request.method == "GET":
            sid = int(request.url.path.rsplit("/", 1)[1])
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "stream": {
                        "stream_id": sid,
                        "name": "30 Work" if sid == 7 else "40 Other",
                        "invite_only": False,
                    },
                },
            )
        assert request.url.path == "/api/v1/users/me/subscriptions/properties"
        assert (
            json.loads(parse_qs(request.content.decode())["subscription_data"][0])
            == changes
        )
        return httpx.Response(200, json={"result": "success"})

    executor = Executor(
        channel_policy=ChannelPolicy(
            jd_enabled=True,
            write_allowlist=frozenset(
                {"30 work", "40 other"} if second_allowed else {"30 work"}
            ),
        ),
        transport=httpx.MockTransport(upstream),
    )
    result = await executor.execute(
        CALLER, "update-subscription-settings", {"subscription_data": changes}
    )
    if second_allowed:
        assert result.error is None
        assert calls == ["GET", "GET", "POST"]
    else:
        assert result.error["code"] == "channel_policy_denied"
        assert calls == ["GET", "GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize("read_only", [True, False])
async def test_bulk_subscription_rejects_readonly_or_invalid_ids_before_io(read_only):
    executor = Executor(
        channel_policy=POLICY,
        read_only=read_only,
        transport=httpx.MockTransport(
            lambda request: pytest.fail("Unexpected request")
        ),
    )
    result = await executor.execute(
        CALLER,
        "update-subscription-settings",
        {
            "subscription_data": [
                {"stream_id": 0, "property": "is_muted", "value": True}
            ],
        },
    )
    assert result.error["code"] == (
        "read_only_policy" if read_only else "invalid_arguments"
    )


@pytest.mark.asyncio
async def test_unsubscribe_preserves_principals_and_checks_channel():
    import json
    from urllib.parse import parse_qs

    writes = []

    def upstream(request):
        if request.method == "DELETE":
            writes.append(request)
            form = parse_qs(request.content.decode())
            assert json.loads(form["subscriptions"][0]) == ["30 Work"]
            assert json.loads(form["principals"][0]) == [22]
            return httpx.Response(
                200, json={"result": "success", "removed": {"22": ["30 Work"]}}
            )
        if request.url.path == "/api/v1/get_stream_id":
            return httpx.Response(200, json={"result": "success", "stream_id": 7})
        return httpx.Response(
            200,
            json={
                "result": "success",
                "stream": {
                    "stream_id": 7,
                    "name": "30 Work",
                    "invite_only": False,
                },
            },
        )

    executor = Executor(channel_policy=POLICY, transport=httpx.MockTransport(upstream))
    result = await executor.execute(
        CALLER, "unsubscribe", {"subscriptions": ["30 Work"], "principals": [22]}
    )
    assert result.error is None
    assert len(writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,method,path,extra",
    [
        ("archive-stream", "DELETE", "/api/v1/streams/7", {}),
        ("delete-topic", "POST", "/api/v1/streams/7/delete_topic", {"topic_name": ""}),
    ],
)
@pytest.mark.parametrize("allowed", [True, False])
async def test_channel_destructive_operations_obey_write_policy(
    operation, method, path, extra, allowed
):
    calls = []

    def upstream(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "stream": {
                        "stream_id": 7,
                        "name": "30 Work",
                        "invite_only": False,
                    },
                },
            )
        assert request.method == method
        assert request.url.path == path
        return httpx.Response(200, json={"result": "success"})

    executor = Executor(
        channel_policy=ChannelPolicy(
            jd_enabled=True,
            write_allowlist=frozenset({"30 work"} if allowed else {"40 other"}),
        ),
        transport=httpx.MockTransport(upstream),
    )
    result = await executor.execute(CALLER, operation, {"stream_id": 7, **extra})
    if allowed:
        assert result.error is None
        assert calls == ["GET", method]
    else:
        assert result.error["code"] == "channel_policy_denied"
        assert calls == ["GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,arguments",
    [
        ("archive-stream", {"stream_id": 7}),
        ("delete-topic", {"stream_id": 7, "topic_name": ""}),
    ],
)
async def test_destructive_channel_readonly_blocks_before_metadata(
    operation, arguments
):
    executor = Executor(
        channel_policy=POLICY,
        read_only=True,
        transport=httpx.MockTransport(
            lambda request: pytest.fail("Unexpected request")
        ),
    )
    result = await executor.execute(CALLER, operation, arguments)
    assert result.error["code"] == "read_only_policy"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes,allowed",
    [
        ({"description": "Updated"}, True),
        ({"new_name": "40 Work"}, True),
        ({"new_name": "general"}, False),
        ({"is_private": True}, False),
    ],
)
async def test_update_channel_checks_current_and_resulting_policy(changes, allowed):
    calls = []

    def upstream(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "stream": {
                        "stream_id": 7,
                        "name": "30 Work",
                        "invite_only": False,
                    },
                },
            )
        assert request.method == "PATCH"
        assert request.url.path == "/api/v1/streams/7"
        return httpx.Response(200, json={"result": "success"})

    executor = Executor(channel_policy=POLICY, transport=httpx.MockTransport(upstream))
    result = await executor.execute(
        CALLER, "update-stream", {"stream_id": 7, **changes}
    )
    if allowed:
        assert result.error is None
        assert calls == ["GET", "PATCH"]
    else:
        assert result.error["code"] == "channel_policy_denied"
        assert calls == ["GET"]


@pytest.mark.asyncio
async def test_channel_rename_cannot_escape_current_write_denial():
    def upstream(request):
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "result": "success",
                "stream": {
                    "stream_id": 7,
                    "name": "30 Work",
                    "invite_only": False,
                },
            },
        )

    executor = Executor(
        channel_policy=ChannelPolicy(
            jd_enabled=True, write_allowlist=frozenset({"40 work"})
        ),
        transport=httpx.MockTransport(upstream),
    )
    result = await executor.execute(
        CALLER, "update-stream", {"stream_id": 7, "new_name": "40 Work"}
    )
    assert result.error["code"] == "channel_policy_denied"


@pytest.mark.asyncio
async def test_topic_visibility_read_only_denies_before_metadata():
    def upstream(request):
        pytest.fail("read-only topic preference reached upstream")

    result = await Executor(
        read_only=True,
        channel_policy=POLICY,
        transport=httpx.MockTransport(upstream),
    ).execute(
        CALLER,
        "update-user-topic",
        {
            "stream_id": 7,
            "topic": "",
            "visibility_policy": 1,
        },
    )
    assert result.error["code"] == "read_only_policy"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["add-default-stream", "remove-default-stream"])
async def test_default_channel_read_only_denies_before_metadata(operation):
    def upstream(request):
        pytest.fail("read-only default channel change reached upstream")

    result = await Executor(
        read_only=True, channel_policy=POLICY, transport=httpx.MockTransport(upstream)
    ).execute(CALLER, operation, {"stream_id": 7})
    assert result.error["code"] == "read_only_policy"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["mark-stream-as-read", "mark-topic-as-read"])
async def test_mark_read_read_only_denies_before_metadata(operation):
    def upstream(request):
        pytest.fail("read-only mark read reached upstream")

    result = await Executor(
        read_only=True, channel_policy=POLICY, transport=httpx.MockTransport(upstream)
    ).execute(
        CALLER,
        operation,
        {
            "stream_id": 7,
            **({"topic_name": ""} if operation == "mark-topic-as-read" else {}),
        },
    )
    assert result.error["code"] == "read_only_policy"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "narrow",
    [
        [],
        [{"operator": "channel", "operand": 7, "negated": True}],
        [{"operator": "channel", "operand": "eng"}],
    ],
)
async def test_narrow_flags_without_positive_channel_id_do_not_write(narrow):
    def upstream(request):
        pytest.fail("unscoped flag change reached upstream")

    result = await Executor(
        channel_policy=POLICY, transport=httpx.MockTransport(upstream)
    ).execute(
        CALLER,
        "update-message-flags-for-narrow",
        {
            "narrow": narrow,
            "anchor": "oldest",
            "num_before": 0,
            "num_after": 100,
            "flag": "starred",
            "op": "remove",
        },
    )
    assert result.error["code"] == "channel_policy_denied"


@pytest.mark.asyncio
async def test_narrow_flags_read_only_denies_before_metadata():
    def upstream(request):
        pytest.fail("readonly flag change reached upstream")

    result = await Executor(
        read_only=True, channel_policy=POLICY, transport=httpx.MockTransport(upstream)
    ).execute(
        CALLER,
        "update-message-flags-for-narrow",
        {
            "narrow": [{"operator": "channel", "operand": 7}],
            "anchor": "oldest",
            "num_before": 0,
            "num_after": 100,
            "flag": "read",
            "op": "add",
        },
    )
    assert result.error["code"] == "read_only_policy"


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_phase", [1, 2])
async def test_subscription_policy_timeout_distinguishes_before_and_after_write(
    monkeypatch, timeout_phase
):
    from contextlib import asynccontextmanager

    import thinkhome_zulip_mcp.executor as module
    from thinkhome_zulip_mcp.executor import OperationResult

    phases = 0
    writes = []

    @asynccontextmanager
    async def timeout(seconds):
        nonlocal phases
        assert seconds == 30
        phases += 1
        if phases == timeout_phase:
            raise TimeoutError
        yield

    async def execute(principal, operation, arguments):
        if operation == "get-stream-id":
            return OperationResult("id", data={"stream_id": 7})
        if operation == "get-stream-by-id":
            return OperationResult(
                "stream",
                data={
                    "stream": {"stream_id": 7, "name": "30 Work", "invite_only": False}
                },
            )
        assert operation == "subscribe"
        writes.append(operation)
        return OperationResult("write", data={"result": "success"})

    executor = Executor(channel_policy=POLICY)
    monkeypatch.setattr(executor, "_execute", execute)
    monkeypatch.setattr(module.asyncio, "timeout", timeout)
    result = await executor.execute(
        CALLER, "subscribe", {"subscriptions": [{"name": "30 Work"}]}
    )
    assert result.error["code"] == "channel_policy_lookup_timeout"
    assert result.uncertain_write is (timeout_phase == 2)
    assert len(writes) == timeout_phase - 1
    assert result.data is None

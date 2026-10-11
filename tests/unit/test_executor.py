"""Exercise real HTTP request encoding against an in-memory upstream transport."""

import base64
import json
from urllib.parse import parse_qs

import httpx
import pytest


@pytest.mark.asyncio
async def test_send_message_form_encoding_and_identity():
    from thinkhome_zulip_mcp.executor import Executor, Principal

    received = []

    def upstream(request):
        received.append(request)
        return httpx.Response(200, json={"result": "success", "msg": "", "id": 17})

    executor = Executor(transport=httpx.MockTransport(upstream))
    result = await executor.execute(
        Principal(
            "alice", "https://zulip.example", "alice@example.test", "synthetic-key"
        ),
        "send-message",
        {"type": "stream", "to": 10, "topic": "test", "content": "hello"},
    )
    assert result.error is None
    assert result.data["id"] == 17
    request = received[0]
    assert request.method == "POST"
    assert str(request.url) == "https://zulip.example/api/v1/messages"
    assert parse_qs(request.content.decode()) == {
        "type": ["stream"],
        "to": ["10"],
        "topic": ["test"],
        "content": ["hello"],
    }
    assert (
        request.headers["authorization"]
        == httpx.BasicAuth("alice@example.test", "synthetic-key")._auth_header
    )


@pytest.mark.asyncio
async def test_unknown_arguments_are_rejected():
    from thinkhome_zulip_mcp.executor import Executor, Principal

    def upstream(request):
        pytest.fail("Invalid input reached upstream")

    executor = Executor(transport=httpx.MockTransport(upstream))
    principal = Principal(
        "alice", "https://zulip.example", "alice@example.test", "fake"
    )
    for arguments in [
        {"type": "stream", "to": 10, "content": "x", "evil": 1},
        {"type": "stream", "to": 10, "content": "x", "topic": "a", "subject": "b"},
        {"type": "stream", "to": 10, "content": "x", "method": "DELETE"},
    ]:
        result = await executor.execute(principal, "send-message", arguments)
        assert result.error["code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_write_timeout_is_not_retried():
    from thinkhome_zulip_mcp.executor import Executor, Principal

    calls = []

    def upstream(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    result = await Executor(transport=httpx.MockTransport(upstream)).execute(
        Principal("a", "https://zulip.example", "a@example.test", "fake"),
        "send-message",
        {"type": "stream", "to": 1, "content": "x"},
    )
    assert len(calls) == 1
    assert result.uncertain_write is True
    assert result.error["code"] == "upstream_transport_error"


@pytest.mark.asyncio
async def test_redirect_does_not_leak_credentials():
    from thinkhome_zulip_mcp.executor import Executor, Principal

    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://other.example/image"})

    result = await Executor(transport=httpx.MockTransport(upstream)).execute(
        Principal("a", "https://zulip.example", "a@example.test", "fake"),
        "get-realm-icon",
        {},
    )
    assert len(calls) == 1
    assert result.data == {"location": "https://other.example/image"}
    assert result.status_code == 302


@pytest.mark.asyncio
async def test_json_query_encoding_and_response_bound():
    from thinkhome_zulip_mcp.executor import Executor, Principal

    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, content=b"x" * 129)

    executor = Executor(transport=httpx.MockTransport(upstream), max_response_bytes=128)
    result = await executor.execute(
        Principal("a", "https://zulip.example", "a@example.test", "fake"),
        "get-messages",
        {
            "anchor": "newest",
            "num_before": 5,
            "num_after": 0,
            "narrow": [{"operator": "search", "operand": "hello"}],
        },
    )
    assert json.loads(seen[0].url.params["narrow"]) == [
        {"operator": "search", "operand": "hello"}
    ]
    assert result.error["code"] == "response_too_large"


@pytest.mark.parametrize(
    "url",
    [
        "https://u:p@zulip.example",
        "https://zulip.example/?x=1",
        "https://zulip.example/#x",
        "http://zulip.example",
    ],
)
def test_principal_rejects_unsafe_url_configuration(url):
    from thinkhome_zulip_mcp.executor import Principal

    with pytest.raises(ValueError):
        Principal("a", url, "a@example.test", "fake")


def test_schema_normalization_preserves_property_names_and_enum_data():
    from jsonschema import Draft7Validator

    from thinkhome_zulip_mcp.executor import _normalize_schema

    schema = {
        "type": "object",
        "properties": {"nullable": {"type": "string"}},
        "required": ["nullable"],
        "additionalProperties": False,
    }
    assert Draft7Validator(_normalize_schema(schema)).is_valid({"nullable": "yes"})
    enum = {"enum": [{"nullable": "literal-data"}]}
    validator = Draft7Validator(_normalize_schema(enum))
    assert validator.is_valid({"nullable": "literal-data"})
    assert not validator.is_valid({})


@pytest.mark.asyncio
async def test_read_retries_respect_retry_after():
    from thinkhome_zulip_mcp.executor import Executor, Principal

    calls, sleeps = [], []

    def upstream(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                429, headers={"Retry-After": "2"}, json={"result": "error"}
            )
        return httpx.Response(200, json={"result": "success", "msg": "available"})

    async def sleep(seconds):
        sleeps.append(seconds)

    result = await Executor(
        transport=httpx.MockTransport(upstream), sleep=sleep
    ).execute(
        Principal("a", "https://zulip.example", "a@example.test", "fake"),
        "check-subdomain-available",
        {"subdomain": "testing"},
    )
    assert result.error is None
    assert len(calls) == 2
    assert sleeps == [2.0]
    assert "authorization" not in calls[0].headers


@pytest.mark.asyncio
async def test_two_principals_never_share_cookies_or_credentials():
    from thinkhome_zulip_mcp.executor import Executor, Principal

    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(
            200, headers={"set-cookie": "session=first"}, json={"result": "success"}
        )

    executor = Executor(transport=httpx.MockTransport(upstream))
    for who in ["alice", "bob"]:
        result = await executor.execute(
            Principal(
                who, "https://zulip.example", who + "@example.test", who + "-key"
            ),
            "get-owned-bots",
            {},
        )
        assert result.error is None
    assert "cookie" not in seen[1].headers
    assert (
        base64.b64decode(seen[1].headers["authorization"][6:]).decode()
        == "bob@example.test:bob-key"
    )


@pytest.mark.asyncio
async def test_plaintext_rate_limit_does_not_retry_before_long_retry_after():
    from thinkhome_zulip_mcp.executor import Executor, Principal

    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"}, text="Slow down")

    async def sleep(seconds):
        pytest.fail("Must return rather than retry before the server deadline")

    result = await Executor(
        transport=httpx.MockTransport(upstream), sleep=sleep
    ).execute(
        Principal("a", "https://zulip.example", "a@example.test", "fake"),
        "check-subdomain-available",
        {"subdomain": "example"},
    )
    assert len(calls) == 1
    assert result.retry_after == "120"


@pytest.mark.asyncio
@pytest.mark.parametrize("email", ["../private", "%252e%252e%252fprivate"])
async def test_path_traversal_never_reaches_upstream(email):
    from thinkhome_zulip_mcp.executor import Executor, Principal

    def upstream(request):
        pytest.fail("Traversal reached upstream")

    result = await Executor(transport=httpx.MockTransport(upstream)).execute(
        Principal("a", "https://zulip.example", "a@example.test", "fake"),
        "get-user-by-email",
        {"email": email},
    )
    assert result.error["code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_nonfinite_upstream_json_is_rejected():
    from thinkhome_zulip_mcp.executor import Executor, Principal

    result = await Executor(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b'{"value":NaN}')
        )
    ).execute(
        Principal("a", "https://zulip.example", "a@example.test", "fake"),
        "get-owned-bots",
        {},
    )
    assert result.error["code"] == "invalid_upstream_response"


@pytest.mark.parametrize(
    "url",
    [
        "https://zulip.example\\x",
        "https://zulip.example\n",
        "https://zulip.example:invalid",
    ],
)
def test_principal_rejects_ambiguous_url_configuration(url):
    from thinkhome_zulip_mcp.executor import Principal

    with pytest.raises(ValueError):
        Principal("a", url, "a@example.test", "fake")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation_id", "prefix"),
    [
        ("get-file-temporary-url", "user_uploads"),
        ("check-thumbnail-status", "thumbnail/status"),
    ],
)
async def test_attachment_path_converter_preserves_segments(operation_id, prefix):
    from thinkhome_zulip_mcp.executor import Executor, Principal

    requests = []

    def upstream(request):
        requests.append(request)
        return httpx.Response(
            200, json={"result": "success", "url": "/user_uploads/temporary/token/name"}
        )

    result = await Executor(transport=httpx.MockTransport(upstream)).execute(
        Principal("a", "https://zulip.example", "a@example.test", "fake"),
        operation_id,
        {"realm_id_str": 1, "filename": "ab/random/žlutý soubor.txt"},
    )
    assert result.error is None
    assert requests[0].url.raw_path == (
        f"/api/v1/{prefix}/1/ab/random/%C5%BElut%C3%BD%20soubor.txt".encode()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename",
    [
        "../x",
        "a/../x",
        "a/./x",
        "a//x",
        "/x",
        "a/",
        "a/%2e%2e/x",
        "a/%252f/x",
        "a/\\x",
        "a/\x00x",
    ],
)
async def test_attachment_path_converter_rejects_traversal(filename):
    from thinkhome_zulip_mcp.executor import Executor, Principal

    def upstream(request):
        pytest.fail("Unsafe path reached upstream")

    result = await Executor(transport=httpx.MockTransport(upstream)).execute(
        Principal("a", "https://zulip.example", "a@example.test", "fake"),
        "get-file-temporary-url",
        {"realm_id_str": 1, "filename": filename},
    )
    assert result.error["code"] == "invalid_arguments"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation, argument, path",
    [
        ("get-user-groups", "include_deactivated_groups", "/api/v1/user_groups"),
        ("get-channel-folders", "include_archived", "/api/v1/channel_folders"),
    ],
)
async def test_zulip_get_form_schemas_are_sent_as_query_parameters(
    operation, argument, path
):
    from thinkhome_zulip_mcp.executor import Executor, Principal

    def upstream(request):
        assert request.method == "GET"
        assert request.url.path == path
        assert request.url.params[argument] == "true"
        assert request.content == b""
        return httpx.Response(200, json={"result": "success"})

    result = await Executor(transport=httpx.MockTransport(upstream)).execute(
        Principal("alice", "https://zulip.example", "alice", "synthetic"),
        operation,
        {argument: True},
    )
    assert result.error is None

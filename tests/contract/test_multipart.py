from email import policy
from email.parser import BytesParser

import httpx
import pytest

from thinkhome_zulip_mcp.executor import Executor, Principal
from thinkhome_zulip_mcp.files import FileStore


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,arguments,field,method,path",
    [
        ("upload-file", {}, "filename", "POST", "/api/v1/user_uploads"),
        (
            "upload-custom-emoji",
            {"emoji_name": "hello"},
            "filename",
            "POST",
            "/api/v1/realm/emoji/hello",
        ),
        ("upload-own-avatar", {}, "file", "POST", "/api/v1/users/me/avatar"),
        ("upload-realm-icon", {}, "file", "POST", "/api/v1/realm/icon"),
        ("upload-realm-logo", {"night": True}, "file", "POST", "/api/v1/realm/logo"),
        (
            "create-bot",
            {
                "full_name": "Test",
                "short_name": "test",
                "payload_url": "https://bot.example",
            },
            "file",
            "POST",
            "/api/v1/bots",
        ),
        ("update-bot", {"bot_id": 7}, "file", "PATCH", "/api/v1/bots/7"),
    ],
)
async def test_multipart_upload_preserves_bytes(
    tmp_path, operation, arguments, field, method, path
):
    principal = Principal("alice", "https://zulip.example", "alice", "synthetic")
    payload = b"\x00\xff\r\nhello"

    async def chunks():
        yield payload[:3]
        yield payload[3:]

    async def upstream(request):
        assert request.method == method
        assert request.url.path == path
        body = await request.aread()
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + body
        )
        parts = {
            part.get_param("name", header="content-disposition"): part
            for part in message.iter_parts()
        }
        assert parts[field].get_payload(decode=True) == payload
        assert parts[field].get_filename() == "sample.bin"
        if operation == "upload-realm-logo":
            assert parts["night"].get_payload(decode=True) == b"true"
        if operation == "create-bot":
            assert (
                parts["payload_url"].get_payload(decode=True)
                == b'"https://bot.example"'
            )
        return httpx.Response(200, json={"result": "success"})

    with FileStore(parent=tmp_path) as files:
        info = await files.stage(
            principal, "sample.bin", "application/octet-stream", chunks()
        )
        executor = Executor(files=files, transport=httpx.MockTransport(upstream))
        result = await executor.execute(
            principal, operation, {**arguments, field: info.handle}
        )
        assert result.error is None
        assert result.status_code == 200


@pytest.mark.asyncio
async def test_multipart_does_not_accept_other_users_handle_or_host_path(tmp_path):
    alice = Principal("alice", "https://zulip.example", "alice", "test")
    bob = Principal("bob", "https://zulip.example", "bob", "test")

    async def chunks():
        yield b"private"

    def no_network(request):
        pytest.fail("Unauthorized file must not reach network")

    with FileStore(parent=tmp_path) as files:
        info = await files.stage(alice, "private", "text/plain", chunks())
        executor = Executor(files=files, transport=httpx.MockTransport(no_network))
        for handle in (info.handle, "/etc/passwd"):
            result = await executor.execute(bob, "upload-file", {"filename": handle})
            assert result.error["code"] == "invalid_file_handle"

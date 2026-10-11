import time

import httpx
import pytest
from cryptography.fernet import Fernet

from thinkhome_zulip_mcp.auth import AuthConfig, Authenticator
from thinkhome_zulip_mcp.executor import Executor, Principal
from thinkhome_zulip_mcp.files import FileStore
from thinkhome_zulip_mcp.server import create_http_app
from thinkhome_zulip_mcp.store import IdentityStore


@pytest.mark.asyncio
async def test_authenticated_file_roundtrip_isolation_limits_and_cleanup(tmp_path):
    store = IdentityStore(tmp_path / "db", Fernet.generate_key())
    tokens = {}
    for user in ("alice", "bob"):
        store.add_identity(Principal(user, "https://zulip.example", user, "synthetic"))
        tokens[user] = store.issue_token(user, expires_at=int(time.time()) + 100)
    files = FileStore(parent=tmp_path, max_bytes=4)
    app = create_http_app(
        Authenticator(
            store, AuthConfig("https://mcp.example/mcp", "https://zulip.example")
        ),
        Executor(files=files),
        json_response=True,
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://mcp.example"
        ) as client,
    ):
        alice = {"authorization": f"Bearer {tokens['alice']}"}
        bob = {"authorization": f"Bearer {tokens['bob']}"}
        denied = await client.post("/mcp/files?filename=x", content=b"x")
        assert denied.status_code == 401
        assert "resource_metadata" in denied.headers["www-authenticate"]
        bad_origin = await client.post(
            "/mcp/files?filename=x",
            headers={**alice, "origin": "https://attacker.example"},
            content=b"x",
        )
        assert bad_origin.status_code == 403
        uploaded = await client.post(
            "/mcp/files?filename=test.bin", headers=alice, content=b"\x00\xffab"
        )
        assert uploaded.status_code == 201, uploaded.text
        handle = uploaded.json()["handle"]
        assert uploaded.json()["size"] == 4
        path = f"/mcp/files/{handle}"
        assert (await client.get(path, headers=bob)).status_code == 404
        read = await client.get(path, headers=alice)
        assert read.content == b"\x00\xffab"
        assert read.headers["content-disposition"].startswith("attachment;")
        assert read.headers["cache-control"] == "no-store"
        assert (
            await client.post(
                "/mcp/files?filename=x", headers=alice, content=b"too large"
            )
        ).status_code == 413
        assert (
            await client.post(
                "/mcp/files?filename=../outside", headers=alice, content=b"x"
            )
        ).status_code == 400

        async def oversized_chunks():
            yield b"123"
            yield b"456"

        assert (
            await client.post(
                "/mcp/files?filename=chunked",
                headers=alice,
                content=oversized_chunks(),
            )
        ).status_code == 413
        assert len(list(files.directory.iterdir())) == 1
        assert (await client.delete(path, headers=bob)).status_code == 404
        assert (await client.delete(path, headers=alice)).status_code == 204
        assert (await client.get(path, headers=alice)).status_code == 404
        store.revoke_token(tokens["alice"])
        assert (
            await client.post("/mcp/files?filename=x", headers=alice, content=b"x")
        ).status_code == 401
    assert not files.directory.exists()


@pytest.mark.asyncio
async def test_download_tool_stages_owned_bytes_for_http_retrieval(tmp_path):
    store = IdentityStore(tmp_path / "db", Fernet.generate_key())
    store.add_identity(Principal("a", "https://zulip.example", "a", "synthetic"))
    token = store.issue_token("a", expires_at=int(time.time()) + 100)

    def upstream(request):
        if request.url.path.startswith("/api/v1/"):
            return httpx.Response(
                200,
                json={"result": "success", "url": "/user_uploads/temporary/TOKEN/name"},
            )
        assert "authorization" not in request.headers
        return httpx.Response(200, content=b"attachment")

    app = create_http_app(
        Authenticator(
            store, AuthConfig("https://mcp.example/mcp", "https://zulip.example")
        ),
        Executor(
            files=FileStore(parent=tmp_path), transport=httpx.MockTransport(upstream)
        ),
        json_response=True,
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://mcp.example"
        ) as client,
    ):
        headers = {
            "authorization": f"Bearer {token}",
            "accept": "application/json, text/event-stream",
        }
        response = await client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        headers["mcp-session-id"] = response.headers["mcp-session-id"]
        headers["mcp-protocol-version"] = response.json()["result"]["protocolVersion"]
        await client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        response = await client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "zulip_download_file",
                    "arguments": {"realm_id_str": 1, "filename": "ab/name"},
                },
            },
        )
        result = response.json()["result"]
        assert not result.get("isError"), result
        handle = result["structuredContent"]["data"]["handle"]
        assert (
            await client.get(f"/mcp/files/{handle}", headers=headers)
        ).content == b"attachment"

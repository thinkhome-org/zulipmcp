import httpx
import pytest

from thinkhome_zulip_mcp.executor import Executor, Principal
from thinkhome_zulip_mcp.files import FileStore


@pytest.mark.asyncio
async def test_download_uses_caller_then_credential_free_signed_transfer(tmp_path):
    from thinkhome_zulip_mcp.downloads import download_file

    seen = []

    def upstream(request):
        seen.append(request)
        if request.url.path.startswith("/api/v1/"):
            assert request.headers["authorization"].startswith("Basic ")
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "url": "/user_uploads/temporary/TOKEN/name.bin",
                },
            )
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        if request.url.host == "zulip.example":
            return httpx.Response(
                302,
                headers={
                    "location": "https://storage.example/object?signature=synthetic",
                    "set-cookie": "secret=x",
                },
            )
        return httpx.Response(
            200,
            content=b"\x00\xffabc",
            headers={"content-type": "application/octet-stream"},
        )

    alice = Principal("alice", "https://zulip.example", "alice", "synthetic")
    with FileStore(parent=tmp_path) as files:
        executor = Executor(files=files, transport=httpx.MockTransport(upstream))
        result = await download_file(
            executor,
            alice,
            1,
            "ab/hash/name.bin",
            allowed_origins=("https://storage.example",),
        )
        assert result.error is None
        assert "TOKEN" not in str(result.data)
        with files.open(alice, result.data["handle"]) as (info, stream):
            assert info.filename == "name.bin"
            assert stream.read() == b"\x00\xffabc"
    assert len(seen) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        "https://attacker.example/file",
        "http://zulip.example/file",
        "https://zulip.example:0/file",
        "https://user:pass@zulip.example/file",
        "https://zulip.example/file#fragment",
        "https://zulip.example/../secret",
    ],
)
async def test_download_refuses_untrusted_redirects(tmp_path, target):
    from thinkhome_zulip_mcp.downloads import download_file

    seen = []

    def upstream(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(
                200,
                json={"result": "success", "url": "/user_uploads/temporary/TOKEN/name"},
            )
        if len(seen) == 2:
            return httpx.Response(302, headers={"location": target})
        pytest.fail("Untrusted redirect fetched")

    with FileStore(parent=tmp_path) as files:
        result = await download_file(
            Executor(files=files, transport=httpx.MockTransport(upstream)),
            Principal("a", "https://zulip.example", "a", "fake"),
            1,
            "ab/name",
        )
        assert result.error is not None
        assert "TOKEN" not in str(result)
        assert list(files.directory.iterdir()) == []


@pytest.mark.asyncio
async def test_download_size_and_upstream_denial(tmp_path):
    from thinkhome_zulip_mcp.downloads import download_file

    calls = []
    deny = False

    def upstream(request):
        calls.append(request)
        if request.url.path.startswith("/api/v1/"):
            if deny:
                return httpx.Response(403, json={"result": "error", "msg": "denied"})
            return httpx.Response(
                200,
                json={"result": "success", "url": "/user_uploads/temporary/TOKEN/name"},
            )
        return httpx.Response(200, content=b"12345")

    with FileStore(parent=tmp_path, max_bytes=4) as files:
        executor = Executor(files=files, transport=httpx.MockTransport(upstream))
        principal = Principal("a", "https://zulip.example", "a", "fake")
        result = await download_file(executor, principal, 1, "ab/name")
        assert result.error["code"] == "file_limit_exceeded"
        assert list(files.directory.iterdir()) == []
        deny = True
        calls.clear()
        result = await download_file(executor, principal, 1, "ab/name")
        assert result.error is not None
        assert len(calls) == 1


def test_download_origin_configuration(monkeypatch):
    from thinkhome_zulip_mcp.downloads import download_origins_from_environment

    monkeypatch.setenv(
        "MCP_DOWNLOAD_ORIGINS", "https://bucket.example,https://cdn.example:8443"
    )
    assert download_origins_from_environment() == (
        "https://bucket.example",
        "https://cdn.example:8443",
    )
    for bad in (
        "http://bucket.example",
        "https://user:pass@bucket.example",
        "https://bucket.example/path",
        "https://bucket.example?secret=x",
        "https://bucket.example#fragment",
        "https://bucket.example:bad",
        " https://bucket.example",
    ):
        monkeypatch.setenv("MCP_DOWNLOAD_ORIGINS", bad)
        with pytest.raises(ValueError):
            download_origins_from_environment()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "bad_initial",
        "redirect_loop",
        "redirect_limit",
        "transport",
        "http_error",
        "chunked_limit",
    ],
)
async def test_download_failure_is_bounded_and_cleans_partial_files(tmp_path, mode):
    from thinkhome_zulip_mcp.downloads import download_file

    calls = 0

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"123"
            yield b"456"

    def upstream(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            url = (
                "https://untrusted.example/file"
                if mode == "bad_initial"
                else "/user_uploads/temporary/TOKEN/name"
            )
            return httpx.Response(200, json={"result": "success", "url": url})
        if mode == "redirect_loop":
            return httpx.Response(302, headers={"location": str(request.url)})
        if mode == "redirect_limit":
            return httpx.Response(
                302, headers={"location": f"/user_uploads/temporary/TOKEN/{calls}"}
            )
        if mode == "transport":
            raise httpx.ReadTimeout("contains-sensitive-signed-url", request=request)
        if mode == "http_error":
            return httpx.Response(403, text="private upstream body")
        return httpx.Response(200, stream=Chunks())

    with FileStore(parent=tmp_path, max_bytes=4) as files:
        result = await download_file(
            Executor(files=files, transport=httpx.MockTransport(upstream)),
            Principal("a", "https://zulip.example", "a", "fake"),
            1,
            "ab/name",
        )
        assert result.error is not None
        assert "TOKEN" not in str(result)
        assert "sensitive" not in str(result)
        assert "private upstream" not in str(result)
        assert calls <= 5
        assert list(files.directory.iterdir()) == []

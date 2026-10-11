"""Resolve caller-authorized attachments, then fetch without API credentials."""

import asyncio
import os
from dataclasses import asdict
from urllib.parse import unquote, urljoin, urlsplit
from uuid import uuid4

import httpx

from thinkhome_zulip_mcp.executor import Executor, OperationResult, Principal
from thinkhome_zulip_mcp.files import FileAccessError, FileLimitError


def _check_url(value: str) -> None:
    if any(ord(c) < 33 for c in value) or "\\" in value:
        raise ValueError("Invalid download URL")
    parsed = urlsplit(value)
    _ = parsed.port
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("Invalid download URL")
    decoded = parsed.path
    for _ in range(4):
        decoded = unquote(decoded)
    if (
        "\\" in decoded
        or "\x00" in decoded
        or any(part in {".", ".."} for part in decoded.split("/"))
    ):
        raise ValueError("Invalid download path")


def _origin(value: str) -> tuple[str, str | None, int]:
    parsed = urlsplit(value)
    return (
        parsed.scheme,
        parsed.hostname,
        parsed.port
        if parsed.port is not None
        else (443 if parsed.scheme == "https" else 80),
    )


def download_origins_from_environment() -> tuple[str, ...]:
    origins = tuple(filter(None, os.environ.get("MCP_DOWNLOAD_ORIGINS", "").split(",")))
    for origin in origins:
        _check_url(origin)
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.path not in {"", "/"}
            or parsed.query
        ):
            raise ValueError("MCP_DOWNLOAD_ORIGINS requires exact HTTPS origins")
    return origins


async def download_file(
    executor: Executor,
    principal: Principal,
    realm_id_str: int,
    filename: str,
    *,
    allowed_origins: tuple[str, ...] = (),
) -> OperationResult:
    request_id = str(uuid4())
    if executor.files is None:
        return OperationResult(request_id, error={"code": "file_store_unavailable"})
    result = await executor.execute(
        principal,
        "get-file-temporary-url",
        {"realm_id_str": realm_id_str, "filename": filename},
    )
    if result.error:
        return result
    request_id = result.request_id
    try:
        target = result.data.get("url") if isinstance(result.data, dict) else None
        if not isinstance(target, str) or not target:
            raise ValueError("Missing temporary URL")
        _check_url(target)
        url = urljoin(principal.realm_url.rstrip("/") + "/", target)
        realm_origin = _origin(principal.realm_url)
        if _origin(url) != realm_origin or not urlsplit(url).path.startswith(
            urlsplit(principal.realm_url).path.rstrip("/") + "/user_uploads/temporary/"
        ):
            raise ValueError("Unexpected temporary URL")
        origins = {realm_origin, *(_origin(origin) for origin in allowed_origins)}
        seen: set[str] = set()
        async with asyncio.timeout(60):
            for _ in range(4):
                _check_url(url)
                if (
                    _origin(url) not in origins
                    or urlsplit(url).scheme != "https"
                    and not (
                        principal.allow_insecure_http and _origin(url) == realm_origin
                    )
                    or url in seen
                ):
                    raise ValueError("Untrusted download URL")
                seen.add(url)
                # A fresh client per hop prevents cookie forwarding, even same-origin.
                async with httpx.AsyncClient(
                    transport=executor.transport,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=30,
                ) as client:
                    async with client.stream("GET", url) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location", "")
                            if not location:
                                raise ValueError("Missing redirect target")
                            _check_url(location)
                            url = urljoin(url, location)
                            continue
                        if response.status_code != 200:
                            return OperationResult(
                                request_id,
                                error={"code": "download_http_error"},
                                status_code=response.status_code,
                            )
                        length = response.headers.get("content-length")
                        if (
                            length is not None
                            and int(length) > executor.files.max_bytes
                        ):
                            raise FileLimitError("File size limit exceeded")
                        info = await executor.files.stage(
                            principal,
                            filename.rsplit("/", 1)[-1],
                            response.headers.get(
                                "content-type", "application/octet-stream"
                            ),
                            response.aiter_bytes(chunk_size=65536),
                        )
                        return OperationResult(
                            request_id, data=asdict(info), status_code=200
                        )
        code = "download_redirect_limit"
    except FileLimitError:
        code = "file_limit_exceeded"
    except FileAccessError:
        code = "invalid_file_metadata"
    except (ValueError, TypeError):
        code = "invalid_download_url"
    except (httpx.HTTPError, TimeoutError):
        code = "download_transport_error"
    except OSError:
        code = "file_io_error"
    return OperationResult(request_id, error={"code": code})

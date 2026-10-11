"""Streaming file endpoints under the same bearer and origin checks as MCP."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import asdict
from urllib.parse import quote

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.middleware.bearer_auth import RequireAuthMiddleware
from mcp.server.auth.routes import build_resource_metadata_url
from mcp.server.transport_security import (
    TransportSecurityMiddleware,
    TransportSecuritySettings,
)
from pydantic import AnyHttpUrl
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from thinkhome_zulip_mcp.auth import Authenticator
from thinkhome_zulip_mcp.files import FileAccessError, FileLimitError, FileStore
from thinkhome_zulip_mcp.store import AuthenticationError


def file_routes(
    auth: Authenticator,
    files: FileStore,
    path: str,
    security: TransportSecuritySettings,
) -> list[Route]:
    guard = TransportSecurityMiddleware(security)

    async def handle(scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        invalid_origin = await guard.validate_request(request, is_post=False)
        if invalid_origin is not None:
            await invalid_origin(scope, receive, send)
            return
        try:
            access = get_access_token()
            if access is None:
                raise AuthenticationError("Authentication required")
            principal = auth.principal(access)
            if request.method == "POST":
                length = request.headers.get("content-length")
                if length is not None and int(length) > files.max_bytes:
                    raise FileLimitError("File size limit exceeded")
                async with asyncio.timeout(60):
                    info = await files.stage(
                        principal,
                        request.query_params.get("filename", ""),
                        request.headers.get("content-type", "application/octet-stream"),
                        request.stream(),
                    )
                response: Response = JSONResponse(asdict(info), status_code=201)
            elif request.method == "DELETE":
                files.remove(principal, request.path_params["handle"])
                response = Response(status_code=204)
            else:
                with files.open(principal, request.path_params["handle"]) as (
                    info,
                    stream,
                ):

                    async def content() -> AsyncIterator[bytes]:
                        while chunk := stream.read(65536):
                            yield chunk

                    response = StreamingResponse(
                        content(),
                        media_type=info.content_type,
                        headers={
                            "Content-Length": str(info.size),
                            "Content-Disposition": "attachment; filename*=UTF-8''"
                            + quote(info.filename, safe=""),
                            "Cache-Control": "no-store",
                            "X-Content-Type-Options": "nosniff",
                        },
                    )
                    await response(scope, receive, send)
                    return
        except AuthenticationError:
            response = JSONResponse({"error": "invalid_token"}, status_code=401)
        except FileLimitError:
            response = JSONResponse({"error": "file_limit_exceeded"}, status_code=413)
        except FileAccessError:
            response = JSONResponse(
                {"error": "file_unavailable"},
                status_code=400 if request.method == "POST" else 404,
            )
        except (TimeoutError, ClientDisconnect):
            response = JSONResponse({"error": "upload_interrupted"}, status_code=408)
        except ValueError:
            response = JSONResponse({"error": "invalid_request"}, status_code=400)
        response.headers["cache-control"] = "no-store"
        await response(scope, receive, send)

    secured = RequireAuthMiddleware(
        handle,
        ["zulip:access"],
        build_resource_metadata_url(AnyHttpUrl(auth.config.resource_url)),
    )
    return [
        Route(path, secured, methods=["POST"]),
        Route(path + "/{handle}", secured, methods=["GET", "DELETE"]),
    ]

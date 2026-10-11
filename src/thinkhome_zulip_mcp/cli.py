"""Command-line entry point."""

import argparse
import os
from importlib.metadata import version
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(prog="thinkhome-zulip-mcp")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {version('thinkhome-zulip-mcp')}",
    )
    commands = parser.add_subparsers(dest="command")
    serve = commands.add_parser("serve", help="Run an MCP transport")
    serve.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    from thinkhome_zulip_mcp.provisioning import add_commands, run

    add_commands(commands)
    args = parser.parse_args()
    if args.command in {"identity", "token", "secrets"}:
        try:
            run(args)
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        return
    if args.command != "serve":
        parser.print_help()
        return
    from thinkhome_zulip_mcp.downloads import download_origins_from_environment

    try:
        download_origins = download_origins_from_environment()
    except ValueError as exc:
        parser.error(str(exc))
    if args.transport == "http":
        import uvicorn

        from thinkhome_zulip_mcp.auth import authenticator_from_environment
        from thinkhome_zulip_mcp.executor import Executor
        from thinkhome_zulip_mcp.files import file_store_from_environment
        from thinkhome_zulip_mcp.server import create_http_app

        try:
            authenticator = authenticator_from_environment()
            app = create_http_app(
                authenticator,
                Executor(files=file_store_from_environment()),
                download_origins=download_origins,
            )
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level="warning",
            access_log=False,
            proxy_headers=False,
        )
        return
    from thinkhome_zulip_mcp.server import create_server, principal_from_environment

    try:
        principal = principal_from_environment()
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    from thinkhome_zulip_mcp.executor import Executor
    from thinkhome_zulip_mcp.files import file_store_from_environment

    root = os.environ.get("ZULIP_FILE_ROOT")
    try:
        files = file_store_from_environment(Path(root) if root else None)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    create_server(
        principal, Executor(files=files), download_origins=download_origins
    ).run(transport="stdio")

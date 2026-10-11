"""Command-line entry point."""

import argparse
from importlib.metadata import version


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
    if args.transport != "stdio":
        parser.error(
            "HTTP transport requires the pending multi-user authorization layer"
        )
    from thinkhome_zulip_mcp.server import create_server, principal_from_environment

    try:
        principal = principal_from_environment()
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    create_server(principal).run(transport="stdio")

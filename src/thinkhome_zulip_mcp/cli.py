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
    parser.parse_args()
    parser.print_help()

# ThinkHome Zulip MCP

In development. The current bootstrap provides packaging and CLI version reporting.
It does not yet provide an MCP server or claim complete Zulip API coverage.

The accepted scope is all callable application API operations of a pinned Zulip
release, including audited undocumented operations, stdio, and authenticated
multi-user Streamable HTTP. No custom frontend or Zulip host administration.

## Development

Requires Python 3.12+ and uv.

```sh
uv sync --frozen
uv run thinkhome-zulip-mcp --version
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts
uv run pytest
uv run python scripts/catalog.py check
uv build
```

See [CONTRIBUTING](CONTRIBUTING.md), the
[design](docs/superpowers/specs/2026-10-11-thinkhome-zulip-mcp-design.md), and the
[implementation plan](docs/superpowers/plans/2026-10-11-thinkhome-zulip-mcp.md).

License and publication targets are pending owner selection; no release is published.

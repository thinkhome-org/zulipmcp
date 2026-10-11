# Third-party material

The project's original code is licensed under MIT. The following included
material retains its upstream license.

## windborne/zulipmcp

- Source: https://github.com/windborne/zulipmcp
- Commit: `10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75`
- License: Apache License 2.0, reproduced in `WINDBORNE-LICENSE`.
- Adapted files: `zulipmcp/core.py` Markdown normalization helpers, included as
  `thinkhome_zulip_mcp/markdown.py`; `tests/test_normalize.py`, included as
  `tests/unit/test_markdown.py`.
- Changes: extracted a standalone module, adjusted imports/type syntax and
  formatting. The two original message-length guard tests were replaced by
  local guard/integration tests. `prepare_message` is a new bounded wrapper.
- Upstream copyright notice is reproduced in `WINDBORNE-NOTICE`: Copyright
  2026 WindBorne Systems.

## Zulip

The bundled Zulip schema and source-derived API inventory have their provenance
in `provenance.json` and retain the license reproduced in `ZULIP-LICENSE`.

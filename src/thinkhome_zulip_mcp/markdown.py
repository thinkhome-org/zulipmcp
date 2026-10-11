"""Zulip Markdown fixes adapted from windborne/zulipmcp (Apache-2.0).

Source: 10b4ef1b9a5c9da5a3f043d1bfa196bfbd2e0d75, zulipmcp/core.py.
Changes: extracted standalone module, local formatting and modern type syntax.
See data/WINDBORNE-LICENSE and data/THIRD-PARTY-NOTICES.md.
"""

import os
import re
from collections.abc import Callable

_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")

_INLINE_CODE_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")

_LINK_RE = re.compile(r"(?<!!)\[([^\]]*)\]\((<?(?:\([^()\s]*\)|[^()\s])+>?)\)")

_URL_ABUTS_BOLD_RE = re.compile(r"(https?://[^\s<>\[\]()*]+)(?=\*\*)")


def _autofix_enabled() -> bool:
    """Whether outgoing markdown normalization is enabled.

    Controlled by ``ZULIPMCP_MARKDOWN_AUTOFIX``: set to ``0`` or ``false``
    (case-insensitive) to disable all normalization.  Read at call time so
    it can be toggled without a restart.
    """
    return os.environ.get("ZULIPMCP_MARKDOWN_AUTOFIX", "").lower() not in ("0", "false")


def _is_table_separator(line: str) -> bool:
    """Whether *line* is a markdown table separator (e.g. ``| --- | --- |``)."""
    stripped = line.strip()
    if not stripped or "-" not in stripped:
        return False
    inner = stripped.lstrip("|").rstrip("|")
    return bool(inner) and set(inner) <= set("|:- ")


def _restyle_bold_in_link(m: "re.Match[str]") -> str:
    """Rewrite bold inside a markdown link's text so Zulip renders it.

    Whole-text bold moves outside the link (``[**a**](url)`` becomes
    ``**[a](url)**``); partial bold is stripped from the text.
    """
    text, url = m.group(1), m.group(2)
    if "**" not in text:
        return m.group(0)
    if text.startswith("**") and text.endswith("**") and text.count("**") == 2:
        return f"**[{text[2:-2]}]({url})**"
    return f"[{text.replace('**', '')}]({url})"


def _sub_outside_code(
    pattern: "re.Pattern[str]", repl: Callable[["re.Match[str]"], str], line: str
) -> str:
    """Apply ``pattern.sub(repl, line)``, leaving matches inside inline code alone.

    A match is exempt only when wholly enclosed in an inline code span; a
    match that merely *contains* a code span is still rewritten.
    """
    code_spans = [span.span() for span in _INLINE_CODE_RE.finditer(line)]

    def guarded(m: "re.Match[str]") -> str:
        if any(s <= m.start() and m.end() <= e for s, e in code_spans):
            return m.group(0)
        return repl(m)

    return pattern.sub(guarded, line)


def _fix_bold_links(line: str) -> str:
    """Rewrite bold/link combinations that Zulip's renderer breaks on."""
    if "**" not in line:
        return line
    line = _sub_outside_code(_LINK_RE, _restyle_bold_in_link, line)
    line = _sub_outside_code(
        _URL_ABUTS_BOLD_RE, lambda m: f"[{m.group(1)}]({m.group(1)})", line
    )
    return line


def normalize_zulip_markdown(content: str) -> str:
    """Normalize outgoing markdown for Zulip's renderer.

    Two deterministic fixes for patterns LLMs trained on GFM emit constantly:

    1. Blank lines before tables.  Zulip's Python-Markdown parser requires a
       blank line before table header rows; injects the missing one.
    2. Bold/link combos Zulip breaks on.  Bold inside link text
       (``[**a**](url)``) renders as literal escaped asterisks with an
       unclickable link (zulip/zulip#36087, fix PR stalled upstream) —
       whole-text bold moves outside the link, partial bold is stripped.
       A bare URL immediately followed by ``**`` is wrapped as
       ``[url](url)``: a trailing ``**`` otherwise gets swallowed into the
       autolinked URL, and a URL *preceded* by ``**`` never autolinks at
       all, so the rewrite also upgrades bold URLs to clickable links.

    Both fixes skip fenced code blocks and indented code.  The bold/link
    fix also skips inline code spans; the table fix also skips blockquotes.
    Accepted edge case: a URL whose path literally contains ``**`` is
    truncated at the asterisks (literal ``*`` in URLs should be
    percent-encoded).

    Set ``ZULIPMCP_MARKDOWN_AUTOFIX=0`` (or ``false``) to disable all
    normalization and send content verbatim.
    """
    if not _autofix_enabled():
        return content

    lines = content.split("\n")
    result: list[str] = []
    in_fence = False
    fence_char: str | None = None
    fence_len = 0

    for i, line in enumerate(lines):
        stripped = line.strip()

        m = _FENCE_RE.match(stripped)
        if m:
            fence_chars = m.group(1)
            after_fence = stripped[len(fence_chars) :]
            if not in_fence:
                in_fence, fence_char, fence_len = True, fence_chars[0], len(fence_chars)
            elif (
                fence_chars[0] == fence_char
                and len(fence_chars) >= fence_len
                and not after_fence
            ):
                in_fence = False
                fence_char = None
            result.append(line)
            continue

        if in_fence:
            result.append(line)
            continue

        if (
            stripped
            and "|" in stripped
            and result
            and result[-1].strip()
            and not stripped.startswith(">")
            and not line.startswith("    ")
            and not line.startswith("\t")
            and i + 1 < len(lines)
            and _is_table_separator(lines[i + 1])
        ):
            result.append("")

        if not line.startswith("    ") and not line.startswith("\t"):
            line = _fix_bold_links(line)

        result.append(line)

    return "\n".join(result)


def prepare_message(content: str, *, max_length: int = 10000) -> str:
    """Bound regex work and check growth from reference Markdown normalization."""
    if len(content) > max_length:
        raise ValueError("Message exceeds configured character limit")
    normalized = normalize_zulip_markdown(content)
    if len(normalized) > max_length:
        raise ValueError("Normalized message exceeds configured character limit")
    return normalized

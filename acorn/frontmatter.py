"""Obsidian-style YAML frontmatter parser (§5.5e-1).

Hand-rolled subset because adding PyYAML for one feature isn't worth the
dep weight. Supports the shapes Obsidian / Jekyll / Hugo / MkDocs use:
flat key→scalar / quoted-string / inline list / block list / number /
ISO date / bool / null. Nested mappings, multiline strings (``|``/``>``)
and YAML anchors are out of scope — they raise FrontmatterParseError.

A document with no leading ``---\\n`` fence returns None (signals "no
frontmatter present"). An empty fenced block returns {}.
"""

from __future__ import annotations

import re
from pathlib import Path

_FENCE = re.compile(r"^(---|\.\.\.)\s*$")


class FrontmatterParseError(Exception):
    """Raised when the leading frontmatter block exists but is malformed.

    Callers in the indexer convert this to "filter doesn't match" so a
    single typo in one note can't abort an index build.
    """


def read_frontmatter_from_text(text: str) -> dict[str, object] | None:
    """Return the parsed frontmatter, ``{}`` if the block is empty, or
    ``None`` if no frontmatter fence appears at the very start of the
    document. Raises FrontmatterParseError on malformed YAML."""
    if not text.startswith("---"):
        return None
    lines = text.splitlines()
    # First line must be exactly ``---`` (allow trailing whitespace).
    if not _FENCE.match(lines[0]):
        return None
    # Find the matching closing fence. The opening line is ``---``; from
    # line 1 onward, look for ``---`` or ``...`` on its own.
    for i in range(1, len(lines)):
        if _FENCE.match(lines[i]):
            body_lines = lines[1:i]
            return _parse_block(body_lines)
    raise FrontmatterParseError("frontmatter block has no closing fence")


def read_frontmatter_from_file(path: Path) -> dict[str, object] | None:
    """Convenience wrapper. Returns None if the file can't be read as
    UTF-8 text — frontmatter only makes sense for text formats."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return read_frontmatter_from_text(text)


def _parse_block(lines: list[str]) -> dict[str, object]:
    """Stub for the next task. Returns {} so the empty-block test passes."""
    if not lines:
        return {}
    raise FrontmatterParseError("frontmatter parsing not yet implemented")

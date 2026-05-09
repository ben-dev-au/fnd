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

import datetime as dt
import re
from pathlib import Path

_FENCE = re.compile(r"^(---|\.\.\.)\s*$")
_KEY_VALUE = re.compile(r"^([A-Za-z_][\w\- ]*?)\s*:\s*(.*)$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
    if not lines:
        return {}
    out: dict[str, object] = {}
    i = 0
    while i < len(lines):
        raw = lines[i]
        # Reject indented continuation that would imply nested mapping —
        # we don't support nested structures.
        if raw and raw[0] in (" ", "\t"):
            raise FrontmatterParseError(
                f"frontmatter line {i + 2}: nested mappings are not supported"
            )
        # Blank lines inside the block are allowed; ignore.
        if not raw.strip():
            i += 1
            continue
        m = _KEY_VALUE.match(raw)
        if not m:
            raise FrontmatterParseError(f"frontmatter line {i + 2}: expected ``key: value``")
        key = m.group(1).rstrip()
        value_text = m.group(2)
        # YAML anchors / aliases / tags — explicit reject.
        if value_text.startswith("&") or value_text.startswith("*") or value_text.startswith("!"):
            raise FrontmatterParseError(
                f"frontmatter line {i + 2}: anchors/aliases/tags are unsupported"
            )
        out[key] = _parse_scalar(value_text)
        i += 1
    return out


def _parse_scalar(text: str) -> object:
    """Coerce one bare value into an int/float/date/bool/None/str.

    List parsing (inline ``[a, b]`` and block lists) is added in the next
    task; for now any ``[`` or block-list marker is treated as a string.
    """
    s = text.strip()
    if not s:
        return ""
    # Quoted strings.
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    # Booleans and null.
    lower = s.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower in ("null", "~"):
        return None
    # ISO date.
    if _ISO_DATE.match(s):
        return dt.date.fromisoformat(s)
    # Number.
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        pass
    # Fallback: bare string.
    return s

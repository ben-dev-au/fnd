"""Fold a chunk's own heading into every representation, or into none.

A PDF page's heading is usually derived from the document TOC rather than
read off the page, so it often isn't present in the page's own text. Baking
it into ``body`` makes the section findable by its heading — but only the
chunk that *starts* the section may do so. ``_toc_heading_for_page`` returns
the same heading for every page of a section, so an unconditional fold made
every continuation page match a heading it neither owns nor renders: search
hit the chunk, the preview painted nothing, and the user landed on blank text.

The rule is therefore twofold:

* **Ownership** — a chunk owns its heading only when its ``heading_path``
  differs from the previously yielded chunk's. Continuation pages and the
  ``carry_heading`` sections that inherit attribution across a page break are
  left untouched; their heading stays searchable through ``F_HEADING_PATH``.
* **Parity** — when a chunk does own its heading, the text goes into all three
  representations (``body`` for search, ``body_struct`` and ``body_md`` for the
  two preview substrates), so anything the index can match, the preview can
  paint.

Ownership is derived from the ordered yield stream rather than from extraction
state, so chunks served from the texture cache follow the identical rule.
"""

from __future__ import annotations

import re

from fnd.extract.base import Block, Chunk

__all__ = ["HeadingFolder"]


def _heading_key(text: str) -> str:
    """Comparable form of a heading: no ATX hashes, no inline emphasis, folded
    case and whitespace.

    The structured extractor commonly emits a page heading with its typographic
    emphasis intact (``## **Interactive Online Learning Environment and Test
    Bank**``) while the TOC gives the plain string. Comparing the two literally
    said "not present" and folded a second copy in, so the preview showed the
    same heading twice.
    """
    return re.sub(r"[*_`~\s]+", " ", text.lstrip("#")).strip().casefold()


def _leading_heading_key(body_md: str) -> str:
    """:func:`_heading_key` of ``body_md``'s first non-blank line when that line
    is an ATX heading, else ``""``."""
    for line in body_md.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return _heading_key(stripped) if stripped.startswith("#") else ""
    return ""


class HeadingFolder:
    """Folds each chunk's own heading in, once, across all representations.

    One instance per document — ownership is decided by comparing against the
    previous chunk, so a shared instance would leak attribution between files.
    """

    __slots__ = ("_prev_heading_path",)

    def __init__(self) -> None:
        self._prev_heading_path: str | None = None

    def fold(self, chunk: Chunk) -> Chunk:
        """Return ``chunk``, with its own heading folded in when it owns one.

        Mutates and returns the chunk (extractors yield freshly-built chunks,
        and the cache path deliberately rewrites its decoded copies in place).
        """
        heading_path = chunk.heading_path
        owns = bool(heading_path) and heading_path != self._prev_heading_path
        self._prev_heading_path = heading_path
        if not owns:
            return chunk
        leaf = heading_path.split(" > ")[-1].strip()
        if not leaf:
            return chunk
        # Compare whole opening lines, never substrings: a substring test would
        # false-match a leaf inside a longer word (leaf "Security" already
        # present in a body opening "Cybersecurity is broad").
        leaf_key = _heading_key(leaf)
        if _heading_key(chunk.body.split("\n", 1)[0]) != leaf_key:
            chunk.body = f"{leaf}\n{chunk.body}"
        if not (chunk.body_struct and _heading_key(chunk.body_struct[0].text) == leaf_key):
            chunk.body_struct.insert(0, Block(kind="h2", text=leaf))
        if chunk.body_md and _leading_heading_key(chunk.body_md) != leaf_key:
            chunk.body_md = f"## {leaf}\n\n{chunk.body_md}"
        return chunk

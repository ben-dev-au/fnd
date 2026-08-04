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

from fnd.extract.base import Block, Chunk

__all__ = ["HeadingFolder"]


def _leading_atx(body_md: str) -> str:
    """Text of ``body_md``'s first non-blank line when it is an ATX heading."""
    for line in body_md.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return stripped.lstrip("#").strip() if stripped.startswith("#") else ""
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
        # Anchored checks, not ``in``: a substring test would false-match a
        # leaf inside a longer word (leaf "Security" in body "Cybersecurity").
        if not chunk.body.startswith(f"{leaf}\n"):
            chunk.body = f"{leaf}\n{chunk.body}"
        if not (chunk.body_struct and chunk.body_struct[0].text.strip() == leaf):
            chunk.body_struct.insert(0, Block(kind="h2", text=leaf))
        if chunk.body_md and _leading_atx(chunk.body_md) != leaf:
            chunk.body_md = f"## {leaf}\n\n{chunk.body_md}"
        return chunk

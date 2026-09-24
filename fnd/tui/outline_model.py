"""The heading outline of one document, built from its decoded chunks.

Everything comes from what the index already stores, so an outline costs no
file read and no decode beyond the preview's own. One strategy per kind:

* heading blocks (md, docx, odt, html, epub, ods): each section chunk opens
  with its heading block, which carries the true level;
* slides (pptx, odp): one entry per slide;
* rendered headings (ipynb markdown cells, texturised PDF pages): headings
  parsed from ``body_md`` with the preview's own parser, so an entry's
  ``ordinal`` is the index of the heading widget the preview draws for it;
* heading paths (PDF): the ``A > B`` path each page was given at extraction.

A PDF's own bookmarks take precedence: they reach the index only as heading
paths, and a multi-level path is proof the file has them. Without bookmarks a
texturised PDF uses the headings its preview renders, and a plain one falls back
to the per-page headings extraction found.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from markdown_it import MarkdownIt

from fnd.display_text import sanitise_display_text

if TYPE_CHECKING:
    from markdown_it.token import Token

    from fnd.query import FileChunk

__all__ = [
    "NO_HEADINGS",
    "NO_OUTLINE",
    "Outline",
    "OutlineEntry",
    "build_outline",
]

NO_HEADINGS = "No headings in this document"
NO_OUTLINE = "No outline for this file type"

_HEADING_KINDS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_PATH_SEP = " > "


@dataclass(frozen=True, slots=True)
class OutlineEntry:
    """One heading. ``ordinal`` is its index among the headings the preview
    renders inside chunk ``chunk_seq`` (-1: the chunk's top). ``at_top``: the
    heading opens its chunk, so its section starts at the chunk's first row."""

    title: str
    depth: int
    chunk_seq: int
    ordinal: int = -1
    locator: str = ""
    at_top: bool = True


@dataclass(frozen=True, slots=True)
class Outline:
    """A document's headings in reading order, or why it has none."""

    entries: tuple[OutlineEntry, ...] = ()
    reason: str = ""
    _seqs: list[int] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_seqs", [e.chunk_seq for e in self.entries])

    def index_for(self, chunk_seq: int, *, headings_passed: int | None = None) -> int | None:
        """The entry whose section holds a position in ``chunk_seq``.

        ``headings_passed`` counts that chunk's rendered headings at or above
        the position; unknown, only the chunk's opening entries qualify.
        """
        lo = bisect.bisect_left(self._seqs, chunk_seq)
        hi = bisect.bisect_right(self._seqs, chunk_seq)
        for i in range(hi - 1, lo - 1, -1):
            entry = self.entries[i]
            if entry.at_top or (headings_passed is not None and entry.ordinal < headings_passed):
                return i
        return lo - 1 if lo > 0 else None

    def within(self, ancestor: int, index: int) -> bool:
        """Whether entry ``index`` is ``ancestor`` or sits in its subtree."""
        if index < ancestor:
            return False
        depth = self.entries[ancestor].depth
        return all(e.depth > depth for e in self.entries[ancestor + 1 : index + 1])


def build_outline(chunks: Sequence[FileChunk]) -> Outline:
    """The outline for one document's chunks, in ``chunk_seq`` order. Safe to
    run off the event loop: it touches nothing but its arguments."""
    if not chunks:
        return Outline(reason=NO_HEADINGS)
    strategy = _STRATEGIES.get(chunks[0].kind)
    if strategy is None:
        return Outline(reason=NO_OUTLINE)
    entries = tuple(strategy(chunks, _Markdown()))
    return Outline(entries, "" if entries else NO_HEADINGS)


def _display(text: str) -> str:
    return " ".join(sanitise_display_text(text).split())


class _Markdown:
    """The preview's parser (Textual's default "gfm-like"), block rules only.

    Headings are block structure, so skipping the inline pass finds exactly the
    headings the preview renders, ten times faster: 235ms against 2.2s over 500
    texturised pages. One per build, so concurrent builds share nothing.
    """

    def __init__(self) -> None:
        self._block = MarkdownIt("gfm-like")
        self._block.core.ruler.disable(
            ["inline", "linkify", "replacements", "smartquotes", "text_join"], ignoreInvalid=True
        )
        self._inline = MarkdownIt("gfm-like")

    def headings(self, source: str) -> tuple[bool, list[tuple[int, str]]]:
        """Whether ``source`` opens with a heading, and ``(level, title)`` of
        every heading in it, empty titles kept so positions match the widgets."""
        tokens = self._block.parse(source)
        found = [
            (int(tok.tag[1]), self.title(tokens[i + 1].content) if i + 1 < len(tokens) else "")
            for i, tok in enumerate(tokens)
            if tok.type == "heading_open"
        ]
        return bool(tokens) and tokens[0].type == "heading_open", found

    def opens_with_heading(self, source: str) -> bool:
        tokens = self._block.parse(source.lstrip("\n").split("\n\n", 1)[0])
        return bool(tokens) and tokens[0].type == "heading_open"

    def title(self, raw: str) -> str:
        """Plain text of a heading's inline markdown."""
        tokens = self._inline.parseInline(raw)
        return _display(_inline_plain(tokens[0].children or [])) if tokens else _display(raw)


def _inline_plain(children: Sequence[Token]) -> str:
    parts: list[str] = []
    for tok in children:
        if tok.type in ("text", "code_inline"):
            parts.append(tok.content)
        elif tok.type in ("softbreak", "hardbreak"):
            parts.append(" ")
        elif tok.type == "image":
            parts.append(_inline_plain(tok.children or []))
    return "".join(parts)


class _LevelStack:
    """Depth for a heading of a given level: under the nearest shallower one."""

    def __init__(self) -> None:
        self._levels: list[int] = []

    def depth(self, level: int) -> int:
        while self._levels and self._levels[-1] >= level:
            self._levels.pop()
        self._levels.append(level)
        return len(self._levels) - 1


def _heading_blocks(chunks: Sequence[FileChunk], md: _Markdown) -> list[OutlineEntry]:
    stack = _LevelStack()
    out: list[OutlineEntry] = []
    for c in chunks:
        if not c.blocks or c.blocks[0].kind not in _HEADING_KINDS:
            continue
        head = c.blocks[0]
        title = md.title(head.text) if c.kind == "md" else _display(head.text)
        if title:
            out.append(OutlineEntry(title, stack.depth(int(head.kind[1])), c.chunk_seq, 0))
    return out


_Rendered = list[tuple["FileChunk", bool, list[tuple[int, str]]]]


def _rendered(chunks: Sequence[FileChunk], md: _Markdown) -> _Rendered:
    return [
        (c, *md.headings(c.body_md))
        for c in chunks
        if c.body_md and not (c.kind == "ipynb" and (not c.blocks or c.blocks[0].kind != "p"))
    ]


def _entries_from_rendered(rendered: _Rendered) -> list[OutlineEntry]:
    stack = _LevelStack()
    out: list[OutlineEntry] = []
    for c, opens, headings in rendered:
        for ordinal, (level, title) in enumerate(headings):
            if title:
                at_top = ordinal == 0 and opens
                depth = stack.depth(level)
                out.append(OutlineEntry(title, depth, c.chunk_seq, ordinal, _page(c), at_top))
    return out


def _rendered_headings(chunks: Sequence[FileChunk], md: _Markdown) -> list[OutlineEntry]:
    return _entries_from_rendered(_rendered(chunks, md))


def _opening_ordinal(c: FileChunk, md: _Markdown) -> int:
    """0 when the chunk's rendered markdown opens with a heading, so a jump lands
    on that heading's row rather than the padding above it; else -1."""
    return 0 if c.body_md and md.opens_with_heading(c.body_md) else -1


def _page(c: FileChunk) -> str:
    if c.kind != "pdf":
        return ""
    label = c.page_label or (str(c.page) if c.page else "")
    return f"p.{label}" if label else ""


def _heading_paths(chunks: Sequence[FileChunk], md: _Markdown) -> list[OutlineEntry]:
    open_path: list[str] = []
    out: list[OutlineEntry] = []
    for c in chunks:
        path = [_display(p) for p in c.heading_path.split(_PATH_SEP)] if c.heading_path else []
        path = [p for p in path if p]
        if not path:
            continue
        common = 0
        while common < min(len(path), len(open_path)) and path[common] == open_path[common]:
            common += 1
        ordinal = _opening_ordinal(c, md) if common < len(path) else -1
        for depth in range(common, len(path)):
            out.append(OutlineEntry(path[depth], depth, c.chunk_seq, ordinal, _page(c)))
        open_path = path
    return out


def _has_bookmarks(chunks: Sequence[FileChunk]) -> bool:
    return any(_PATH_SEP in c.heading_path for c in chunks)


def _pdf(chunks: Sequence[FileChunk], md: _Markdown) -> list[OutlineEntry]:
    if _has_bookmarks(chunks):
        return _heading_paths(chunks, md)
    rendered = _rendered(chunks, md)
    if any(headings for _c, _opens, headings in rendered):
        return _entries_from_rendered(rendered)
    return _heading_paths(chunks, md)


def _slides(chunks: Sequence[FileChunk], md: _Markdown) -> list[OutlineEntry]:
    seen: set[int] = set()
    out: list[OutlineEntry] = []
    for c in chunks:
        if c.slide in seen:
            continue
        seen.add(c.slide)
        title = _display(c.heading_path) or f"Slide {c.slide}"
        out.append(OutlineEntry(title, 0, c.chunk_seq, _opening_ordinal(c, md), f"s.{c.slide}"))
    return out


_Strategy = Callable[[Sequence["FileChunk"], _Markdown], list[OutlineEntry]]

_STRATEGIES: dict[str, _Strategy] = {
    "md": _heading_blocks,
    "docx": _heading_blocks,
    "odt": _heading_blocks,
    "html": _heading_blocks,
    "epub": _heading_blocks,
    "ods": _heading_blocks,
    "pdf": _pdf,
    "pptx": _slides,
    "odp": _slides,
    "ipynb": _rendered_headings,
}

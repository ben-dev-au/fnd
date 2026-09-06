"""Which rendered rows of a block its matches paint on.

A block's top row and its match's row are different numbers as soon as the block
wraps: measured on a PDF contents page, a 63-row paragraph carries its match on
row 32, so anchoring on the block top drops the match a screenful below the fold.
A block taller than the viewport also holds more than one match — a 166-row
fence carried 12 — so the stop set needs every row, not just the first.

Both substrates resolve the rows here — the live scroll
(:mod:`fnd.tui.preview_scroll`) and the frozen capture
(:mod:`fnd.tui.preview.frozen`) — so a chunk lands in the same place whether or
not it has been captured yet.
"""

from __future__ import annotations

from bisect import bisect_right
from typing import TYPE_CHECKING

from textual.geometry import Region

if TYPE_CHECKING:
    from textual.widget import Widget

    from fnd.matching import MatchSpec

# Hardcoded by ``Content.render_strips``, which every Visual render reaches.
_TAB_SIZE = 8


def _expand_tabs(line: str) -> tuple[str, list[tuple[int, int, int]]]:
    """``line`` with tabs expanded by CELL (Textual's rule, so a wide character
    before a tab moves the stop), and ``(source, expanded, text)`` index triples
    so a character offset maps into the result."""
    from textual.expand_tabs import get_tab_widths

    out: list[str] = []
    marks: list[tuple[int, int, int]] = []
    src = exp = 0
    for text, pad in get_tab_widths(line, _TAB_SIZE):
        marks.append((src, exp, len(text)))
        out.append(text)
        out.append(" " * pad)
        src += len(text) + (1 if pad else 0)  # the tab itself
        exp += len(text) + pad
    return "".join(out), marks


def _expanded_col(marks: list[tuple[int, int, int]], offset: int) -> int:
    """``offset`` (an index into the source line) as an index into the expanded
    one. An offset on the tab itself maps to where the tab began."""
    col = offset
    for src, exp, width in marks:
        if src > offset:
            break
        col = exp + min(offset - src, width)
    return col


def region_at_row(region: Region, row: int) -> Region:
    """``region`` trimmed to begin ``row`` rows down — a block's match rather
    than the block itself."""
    if row <= 0:
        return region
    return Region(region.x, region.y + row, region.width, max(1, region.height - row))


def block_plain(widget: Widget) -> str | None:
    """A widget's rendered text — ``_content`` for every ``MarkdownBlock``, and
    ``.code`` for the non-block descendants ``_fallback_match_target`` scans."""
    try:
        plain = widget._content.plain  # type: ignore[attr-defined]
    except Exception:
        plain = None
    if plain is None:
        return getattr(widget, "code", None)
    return plain


def _match_offsets(block: Widget, plain: str, spec: MatchSpec | None) -> list[int]:
    """Ascending character offsets of the block's matches, from the baked spans
    where it has them, else a scan of ``spec``. Dim spans are a fallback for a
    block with no full match, never an addition to one."""
    spans = getattr(block, "_fnd_match_spans", None)
    if spans:
        from fnd.render import DIM_STYLES

        full = [s.start for s in spans if str(s.style) not in DIM_STYLES]
        return sorted(set(full or [s.start for s in spans]))
    if spec is None or spec.is_empty:
        return []
    from fnd.matching import phrase_char_spans
    from fnd.render import match_word_spans

    starts = [a for a, _b, _style in match_word_spans(plain, spec)]
    starts += [a for a, _b in phrase_char_spans(plain, spec)]
    return sorted(set(starts))


def _rows_for_offsets(plain: str, offsets: list[int], width: int, height: int) -> list[int] | None:
    """The rendered rows ``offsets`` (ascending) fall on, or ``None`` when
    neither model reproduces ``height``.

    Wrapping only ADDS rows, so a height equal to the source-line count proves
    nothing wrapped — and the unwrapped model costs nothing against 4.9ms for
    the wrap engine on a paragraph at the structural build cap. Every offset is
    resolved in the one walk, so a block with many matches costs the same
    ``divide_line`` pass as a block with one.

    Tabs expand to ``_TAB_SIZE`` first, per line, because that is what
    ``Content._wrap_and_format`` does before dividing, and by CELL, which is why
    the expansion goes through Textual's own helper. Fences reach the wrapped
    model only because ``FNDApp.CSS`` zeroes
    ``MarkdownFence > Label``'s padding; under stock padding no model reproduces
    the height and this declines.
    """
    from rich._wrap import divide_line

    lines = plain.split("\n")
    tabbed = "\t" in plain
    expansions = [_expand_tabs(line) if tabbed else (line, []) for line in lines]
    for wrap_width in (0, width):
        row = 0
        pos = 0
        i = 0
        found: list[int] = []
        for line, (expanded, marks) in zip(lines, expansions, strict=True):
            breaks = divide_line(expanded, wrap_width) if wrap_width > 0 else []
            while i < len(offsets) and offsets[i] <= pos + len(line):
                column = offsets[i] - pos
                if tabbed:
                    column = _expanded_col(marks, column)
                found.append(row + bisect_right(breaks, column))
                i += 1
            row += 1 + len(breaks)
            pos += len(line) + 1
        if row == height:
            return found
    return None


def _match_rows(block: Widget, spec: MatchSpec | None) -> list[int]:
    """Rows of ``block``'s outer region that carry a match, ascending and
    de-duplicated; empty when the text, the geometry or the wrap model
    declines."""
    plain = block_plain(block)
    if not plain:
        return []
    try:
        content = block.content_region
        outer = block.region
    except Exception:
        return []
    if content.height <= 0 or outer.height <= 0:
        return []
    offsets = _match_offsets(block, plain, spec)
    if not offsets:
        return []
    # Cached per block: ``enumerate_stop_regions`` asks every mounted match block
    # on every n/b press — 56 wrapped 4,000-char paragraphs cost 13.8ms cold and
    # 0.03ms after. Geometry is in the key, so a re-wrap recomputes.
    key = (content.width, content.height, len(plain), tuple(offsets))
    cached: tuple[object, list[int] | None] | None = getattr(block, "_fnd_match_row_cache", None)
    if cached is not None and cached[0] == key:
        rows = cached[1]
    else:
        rows = _rows_for_offsets(plain, offsets, content.width, content.height)
        block._fnd_match_row_cache = (key, rows)  # type: ignore[attr-defined]
    if rows is None:
        return []
    top_pad = max(0, content.y - outer.y)
    return sorted({r + top_pad for r in rows if 0 <= r + top_pad < outer.height})


def rows_to_first_match(block: Widget, spec: MatchSpec | None = None) -> int:
    """Rendered rows from ``block``'s top down to its first match's row, or
    ``0`` when that cannot be established — the block's top is the safe anchor."""
    rows = _match_rows(block, spec)
    return rows[0] if rows else 0


def rows_to_matches(block: Widget, spec: MatchSpec | None = None) -> list[int]:
    """Rendered rows of ``block``'s matches, one per row; ``[0]`` when they
    cannot be established, the same safe anchor :func:`rows_to_first_match`
    falls back to."""
    return _match_rows(block, spec) or [0]


def row_within(widget: Widget, chunk: Widget) -> int | None:
    """``widget``'s top row relative to ``chunk``'s, or ``None`` when either has
    no geometry."""
    try:
        r, c = widget.region, chunk.region
    except Exception:
        return None
    if r.height == 0 or c.height == 0:
        return None
    return r.y - c.y


def chunk_stop_rows(
    chunk: Widget, spec: MatchSpec
) -> tuple[list[int], dict[tuple[int, int, int], int]]:
    """Every row of ``chunk`` a match paints on, sorted, plus the table-cell rows
    that contributed. Ordered by ROW, never by ``match_blocks``, which a table
    joins at mount and so ends whatever follows it in the document."""
    from textual.widgets import DataTable

    from fnd.tui.widgets.markdown import (
        FNDMarkdown,
        FNDMarkdownTableDT,
        FNDMarkdownTD,
        FNDMarkdownTH,
    )

    if not isinstance(chunk, FNDMarkdown):
        return [], {}
    rows: list[int] = []
    for block in chunk.match_blocks:
        # The table owns its cells' rows (resolved below); counting the phantom
        # cell blocks as well double-counts every table match.
        if isinstance(block, FNDMarkdownTableDT | FNDMarkdownTD | FNDMarkdownTH):
            continue
        row = row_within(block, chunk)
        if row is not None:
            rows.extend(row + r for r in rows_to_matches(block, spec))
    # Keyed by TABLE as well as cell: two tables in one chunk can match the same
    # local coordinate, and collapsing them drops the first one's row entirely.
    cells: dict[tuple[int, int, int], int] = {}
    for index, dt in enumerate(chunk.query(DataTable)):
        base = row_within(dt, chunk)
        if base is None:
            continue
        for coord in getattr(dt, "_fnd_match_coords", []) or []:
            try:
                cell = dt._get_cell_region(coord)  # pyright: ignore[reportAttributeAccessIssue]
            except Exception:
                continue
            if cell.height == 0:
                continue
            row = base + cell.y - dt.scroll_offset.y
            cells[(index, coord.row, coord.column)] = row
            rows.append(row)
    return sorted(set(rows)), cells

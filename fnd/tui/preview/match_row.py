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
    """Ascending character offsets of the block's matches, preferring the baked
    highlight spans and their full-over-dimmed tiering to a scan of ``spec``.

    The dim tier is a fallback for a block with no full match, never an addition
    to one — the same rule ``first_match_block`` applies.
    """
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
    ``Content._wrap_and_format`` does before dividing. ``str.expandtabs`` sets
    its stops by character where Textual sets them by cell: over 66,652 tabbed
    lines of one index, 20 part, none of them by a row COUNT — so the height
    check passes them and a match in the one shifted break column lands a row
    out. Fences reach the wrapped model only because ``FNDApp.CSS`` zeroes
    ``MarkdownFence > Label``'s padding; under stock padding no model reproduces
    the height and this declines.
    """
    from rich._wrap import divide_line

    lines = plain.split("\n")
    tabbed = "\t" in plain
    for wrap_width in (0, width):
        row = 0
        pos = 0
        i = 0
        found: list[int] = []
        for line in lines:
            expanded = line.expandtabs(_TAB_SIZE) if tabbed else line
            breaks = divide_line(expanded, wrap_width) if wrap_width > 0 else []
            while i < len(offsets) and offsets[i] <= pos + len(line):
                column = offsets[i] - pos
                if tabbed:
                    column = len(line[:column].expandtabs(_TAB_SIZE))
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
    """Rendered rows from ``block``'s top down to each of its matches, one per
    row. ``[0]`` when they cannot be established, so a caller always gets the
    same safe anchor :func:`rows_to_first_match` returns."""
    return _match_rows(block, spec) or [0]

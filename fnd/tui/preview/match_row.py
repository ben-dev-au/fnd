"""Which rendered row of a block its first match paints on.

A block's top row and its match's row are different numbers as soon as the block
wraps: measured on a PDF contents page, a 63-row paragraph carries its match on
row 32, so anchoring on the block top drops the match a screenful below the fold.

Both substrates resolve the row here — the live scroll
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


def region_at_row(region: Region, row: int) -> Region:
    """``region`` trimmed to begin ``row`` rows down — a block's match rather
    than the block itself."""
    if row <= 0:
        return region
    return Region(region.x, region.y + row, region.width, max(1, region.height - row))


def block_plain(widget: Widget) -> str | None:
    """A widget's rendered text.

    ``_content`` for every ``MarkdownBlock`` a mounted chunk holds, fences
    included. The ``.code`` fallback is for the arbitrary descendants
    ``_fallback_match_target`` scans, which are not all markdown blocks.
    """
    try:
        plain = widget._content.plain  # type: ignore[attr-defined]
    except Exception:
        plain = None
    if plain is None:
        return getattr(widget, "code", None)
    return plain


def _first_match_offset(block: Widget, plain: str, spec: MatchSpec | None) -> int | None:
    """Character offset of the block's first match within ``plain``.

    Prefers the highlight spans the block already carries: those are what the
    user can see, and their tiering matches ``first_match_block``'s, so a
    proximity-dimmed stray never wins over a real hit.
    """
    spans = getattr(block, "_fnd_match_spans", None)
    if spans:
        from fnd.render import DIM_STYLES

        full = [s.start for s in spans if str(s.style) not in DIM_STYLES]
        return min(full or [s.start for s in spans])
    if spec is None or spec.is_empty:
        return None
    from fnd.matching import phrase_char_spans
    from fnd.render import match_word_spans

    starts = [a for a, _b, _style in match_word_spans(plain, spec)]
    starts += [a for a, _b in phrase_char_spans(plain, spec)]
    return min(starts) if starts else None


def _row_for_offset(plain: str, offset: int, width: int, height: int) -> int | None:
    """The rendered row ``offset`` falls on, or ``None`` when neither model
    reproduces ``height`` — the block does not lay out the way this counts, and
    a wrong row is worse than none.

    Wrapping only ever ADDS rows, so a height equal to the source-line count
    proves nothing wrapped: trying that model first settles every unwrapped
    block without touching the wrap engine, which costs 4.9ms on a paragraph at
    the structural build cap.

    Exact except for tabs, which Textual expands before dividing and this does
    not: measured 2 rows wrong in 4,000 tab-indented fences, by 1 row. Fences
    reach the wrapped model at all only because ``FNDApp.CSS`` zeroes
    ``MarkdownFence > Label``'s padding; with stock padding the block's content
    width is not the width the text wraps at, no model reproduces the height,
    and this correctly declines.
    """
    from rich._wrap import divide_line

    for wrap_width in (0, width):
        row = 0
        found: int | None = None
        pos = 0
        for line in plain.split("\n"):
            breaks = divide_line(line, wrap_width) if wrap_width > 0 else []
            if found is None and pos <= offset <= pos + len(line):
                found = row + bisect_right(breaks, offset - pos)
            row += 1 + len(breaks)
            pos += len(line) + 1
        if row == height:
            return found
    return None


def rows_to_first_match(block: Widget, spec: MatchSpec | None = None) -> int:
    """Rendered rows from ``block``'s top down to the row its first match paints
    on. ``0`` whenever that cannot be established — the block's top is then the
    safe anchor, and the caller's clamp treats both the same."""
    plain = block_plain(block)
    if not plain:
        return 0
    try:
        content = block.content_region
        outer = block.region
    except Exception:
        return 0
    if content.height <= 0 or outer.height <= 0:
        return 0
    offset = _first_match_offset(block, plain, spec)
    if offset is None:
        return 0
    # Cached per block: ``enumerate_stop_regions`` asks every mounted match block
    # on every n/b press — 56 wrapped 4,000-char paragraphs cost 13.8ms cold and
    # 0.03ms after. Geometry is in the key, so a re-wrap recomputes.
    key = (content.width, content.height, len(plain), offset)
    cached: tuple[object, int | None] | None = getattr(block, "_fnd_match_row_cache", None)
    if cached is not None and cached[0] == key:
        row = cached[1]
    else:
        row = _row_for_offset(plain, offset, content.width, content.height)
        block._fnd_match_row_cache = (key, row)  # type: ignore[attr-defined]
    if row is None:
        return 0
    row += max(0, content.y - outer.y)
    return row if 0 < row < outer.height else 0

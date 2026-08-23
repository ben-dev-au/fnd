"""A block's match row is counted in RENDERED rows, not source newlines.

A wrapped block renders many rows from one source line, so a newline count
reports 0 and the anchor lands on the block's top — measured on a real PDF
contents page, a 63-row paragraph whose match paints on row 32.
"""

from __future__ import annotations

from typing import Any, cast

from textual.geometry import Region
from textual.widget import Widget

from fnd.matching import MatchSpec
from fnd.tui.preview.match_row import rows_to_first_match


class _Block:
    """The geometry and text slice ``rows_to_first_match`` reads."""

    def __init__(self, plain: str, *, width: int, height: int, top_pad: int = 0) -> None:
        class _Content:
            def __init__(self, p: str) -> None:
                self.plain = p

        self._content = _Content(plain)
        self.region = Region(0, 0, width, height + top_pad)
        self.content_region = Region(0, top_pad, width, height)


def _block(plain: str, *, width: int, height: int, top_pad: int = 0) -> Widget:
    return cast(Widget, _Block(plain, width=width, height=height, top_pad=top_pad))


_SPEC = MatchSpec.from_query("glimmer")


def _wrapped(
    match_word_index: int,
    *,
    words: int = 300,
    width: int = 30,
    height: int | None = None,
    top_pad: int = 0,
) -> Widget:
    """A single-source-line paragraph that wraps."""
    parts = [f"word{i:03d}" for i in range(words)]
    parts[match_word_index] = "glimmer"
    # 8 cells a word including the space; width 30 fits 3 (the 4th overflows).
    rows = -(-words // 3) if height is None else height
    return _block(" ".join(parts), width=width, height=rows, top_pad=top_pad)


def test_wrapped_prose_anchors_on_the_row_the_match_paints_on() -> None:
    assert rows_to_first_match(_wrapped(120), _SPEC) == 40


def test_a_match_in_the_first_wrapped_row_needs_no_offset() -> None:
    assert rows_to_first_match(_wrapped(1), _SPEC) == 0


def test_a_fence_still_counts_one_row_per_source_line() -> None:
    lines = [f"filler line {i}" for i in range(120)]
    lines[87] = "    result = glimmer(value)"
    block = _block("\n".join(lines), width=200, height=120)

    assert rows_to_first_match(block, _SPEC) == 87


def test_no_offset_when_the_model_disagrees_with_the_laid_out_height() -> None:
    """A height the wrap model cannot reproduce means the block does not lay out
    the way this counts, and a wrong row is worse than the block's top."""
    assert rows_to_first_match(_wrapped(120, height=50), _SPEC) == 0


def test_padding_above_the_content_shifts_the_row() -> None:
    assert rows_to_first_match(_wrapped(120, top_pad=2), _SPEC) == 42


def test_highlight_spans_win_over_a_spec_scan() -> None:
    """The spans are what the user can SEE, so they decide the row — the scan is
    only the fallback for a block that carries none."""
    from textual.content import Span

    from fnd.render import HIGHLIGHT_STYLE

    # The literal word sits at word 20 (row 6); the span points at word 120
    # (row 40). Only a spans-first read lands on 40.
    block = _wrapped(20)
    block._fnd_match_spans = [Span(120 * 8, 120 * 8 + 7, HIGHLIGHT_STYLE)]  # type: ignore[attr-defined]

    assert rows_to_first_match(block, _SPEC) == 40


def test_a_dimmed_stray_never_beats_a_real_hit() -> None:
    """Mirrors ``first_match_block``'s tiering: a proximity-dimmed span is not
    where the user is being sent."""
    from textual.content import Span

    from fnd.render import DIM_STYLES, HIGHLIGHT_STYLE

    block = _wrapped(120)
    block._fnd_match_spans = [  # type: ignore[attr-defined]
        Span(0, 7, sorted(DIM_STYLES)[0]),
        Span(120 * 8, 120 * 8 + 7, HIGHLIGHT_STYLE),
    ]

    assert rows_to_first_match(block, _SPEC) == 40


def test_no_match_means_no_offset() -> None:
    assert rows_to_first_match(_wrapped(120), MatchSpec.from_query("kubernetes")) == 0


def test_empty_block_means_no_offset() -> None:
    assert rows_to_first_match(_block("", width=30, height=1), _SPEC) == 0


def test_a_block_without_geometry_is_declined() -> None:
    class _Bare:
        def __init__(self) -> None:
            class _Content:
                plain = "a line mentioning glimmer"

            self._content = _Content()

    assert rows_to_first_match(cast(Any, _Bare()), _SPEC) == 0

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
from fnd.tui.preview.match_row import rows_to_first_match, rows_to_matches


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
    """A height no model reproduces falls back to the block's top."""
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


def test_every_match_in_a_block_gets_a_row() -> None:
    """A block taller than the viewport hides its later matches behind its
    first; one stop per block leaves them unreachable by n/b."""
    lines = [f"filler line {i}" for i in range(120)]
    lines[12] = "    a = glimmer(1)"
    lines[87] = "    result = glimmer(value)"
    block = _block("\n".join(lines), width=200, height=120)

    assert rows_to_matches(block, _SPEC) == [12, 87]


def test_the_first_row_is_the_one_the_landing_uses() -> None:
    """``rows_to_first_match`` and ``rows_to_matches`` cannot disagree on where
    a result lands."""
    lines = [f"filler line {i}" for i in range(120)]
    lines[12] = "    a = glimmer(1)"
    lines[87] = "    result = glimmer(value)"
    block = _block("\n".join(lines), width=200, height=120)

    assert rows_to_matches(block, _SPEC)[0] == rows_to_first_match(block, _SPEC)


def test_matches_sharing_a_rendered_row_are_one_stop() -> None:
    """Two stops on one row would make the second press a no-op."""
    lines = [f"filler line {i}" for i in range(120)]
    lines[40] = "    glimmer(glimmer)"
    block = _block("\n".join(lines), width=200, height=120)

    assert rows_to_matches(block, _SPEC) == [40]


def test_wrapped_prose_rows_every_match_it_paints() -> None:
    """Wrapping is where a block's rows and its source lines come apart."""
    parts = [f"word{i:03d}" for i in range(300)]
    parts[120] = "glimmer"
    parts[240] = "glimmer"
    block = _block(" ".join(parts), width=30, height=100)

    assert rows_to_matches(block, _SPEC) == [40, 80]


def test_dimmed_strays_never_join_a_real_hit() -> None:
    """Mirrors ``rows_to_first_match``' tiering: the dim tier is a fallback for
    a block with no full match, never an addition to one."""
    from textual.content import Span

    from fnd.render import DIM_STYLES, HIGHLIGHT_STYLE

    block = _wrapped(120)
    block._fnd_match_spans = [  # type: ignore[attr-defined]
        Span(0, 7, sorted(DIM_STYLES)[0]),
        Span(120 * 8, 120 * 8 + 7, HIGHLIGHT_STYLE),
    ]

    assert rows_to_matches(block, _SPEC) == [40]


def test_dimmed_strays_stand_in_when_nothing_is_full() -> None:
    from textual.content import Span

    from fnd.render import DIM_STYLES

    dim = sorted(DIM_STYLES)[0]
    block = _wrapped(120)
    block._fnd_match_spans = [  # type: ignore[attr-defined]
        Span(40 * 8, 40 * 8 + 7, dim),
        Span(120 * 8, 120 * 8 + 7, dim),
    ]

    assert rows_to_matches(block, _SPEC) == [13, 40]


def test_a_declined_model_falls_back_to_the_block_top() -> None:
    assert rows_to_matches(_wrapped(120, height=50), _SPEC) == [0]


def test_no_match_falls_back_to_the_block_top() -> None:
    assert rows_to_matches(_wrapped(120), MatchSpec.from_query("kubernetes")) == [0]


def test_tabs_expand_the_way_textual_expands_them() -> None:
    """Textual sets tab stops by CELL, so a double-width character before a tab
    moves them. Checked against Textual's own helper, which is the contract."""
    from textual.expand_tabs import expand_tabs_inline

    from fnd.tui.preview.match_row import _expand_tabs

    for line in (
        "识别\tvalue = 1",  # wide characters before the tab move the stop
        "ab\tcd\tef",  # successive stops accumulate
        "\tleading",
        "é́\tcombining marks",
        "no tabs here",
        "",
    ):
        assert _expand_tabs(line)[0] == expand_tabs_inline(line, 8), repr(line)


def test_an_offset_maps_into_the_expanded_line() -> None:
    """A match's column has to be found in the EXPANDED line, or it is compared
    against break positions measured in a different string."""
    from fnd.tui.preview.match_row import _expand_tabs, _expanded_col

    line = "识别\tvalue"
    expanded, marks = _expand_tabs(line)

    assert expanded.startswith("识别    ")  # 4 cells used, so the stop is 4 on
    assert _expanded_col(marks, 0) == 0  # first character
    assert _expanded_col(marks, 2) == 2  # the tab itself maps to where it began
    assert _expanded_col(marks, 3) == 6  # first character after the expansion
    assert expanded[_expanded_col(marks, 3)] == "v"

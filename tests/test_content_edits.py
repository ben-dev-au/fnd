"""Pin: Content text edits remap the spans that survive them."""

from __future__ import annotations

import pytest
from textual.content import Content, Span

from fnd.tui.widgets.content_edits import Edit, apply_edits


def test_no_edits_returns_content_unchanged() -> None:
    c = Content("hello world", spans=[Span(0, 5, "bold")])
    assert apply_edits(c, []) is c


def test_deletion_shifts_a_later_span_left() -> None:
    c = Content("==hi== there", spans=[Span(7, 12, "bold")])
    out = apply_edits(c, [Edit(0, 6, "hi")])
    assert out.plain == "hi there"
    assert [(s.start, s.end, str(s.style)) for s in out.spans] == [(3, 8, "bold")]


def test_span_wholly_inside_a_deletion_is_dropped() -> None:
    c = Content("keep %%gone%% keep", spans=[Span(7, 11, "bold")])
    out = apply_edits(c, [Edit(5, 13, "")])
    assert out.plain == "keep  keep"
    assert list(out.spans) == []


def test_span_overlapping_a_deletion_is_clamped() -> None:
    c = Content("abcdef", spans=[Span(1, 5, "bold")])
    out = apply_edits(c, [Edit(2, 4, "")])
    assert out.plain == "abef"
    assert [(s.start, s.end) for s in out.spans] == [(1, 3)]


def test_replacement_longer_than_the_original_shifts_right() -> None:
    c = Content("[[a]] tail", spans=[Span(6, 10, "bold")])
    out = apply_edits(c, [Edit(0, 5, "alias (a)")])
    assert out.plain == "alias (a) tail"
    assert [(s.start, s.end) for s in out.spans] == [(10, 14)]


def test_edit_styles_land_at_replacement_relative_offsets() -> None:
    c = Content("x [[t|al]] y")
    out = apply_edits(c, [Edit(2, 10, "al (t)", ((0, 2, "cyan"), (3, 3, "dim")))])
    assert out.plain == "x al (t) y"
    assert [(s.start, s.end, str(s.style)) for s in out.spans] == [
        (2, 4, "cyan"),
        (5, 8, "dim"),
    ]


def test_multiple_edits_compose_left_to_right() -> None:
    c = Content("==a== mid ==b==", spans=[Span(6, 9, "bold")])
    out = apply_edits(c, [Edit(0, 5, "a"), Edit(10, 15, "b")])
    assert out.plain == "a mid b"
    assert [(s.start, s.end) for s in out.spans] == [(2, 5)]


def test_edits_are_sorted_before_application() -> None:
    c = Content("==a== mid ==b==")
    out = apply_edits(c, [Edit(10, 15, "b"), Edit(0, 5, "a")])
    assert out.plain == "a mid b"


def test_overlapping_edits_raise_value_error() -> None:
    c = Content("abcdefghij")
    with pytest.raises(ValueError, match=r"\(3, 8\)"):
        apply_edits(c, [Edit(0, 5, "x"), Edit(3, 8, "y")])


def test_span_end_inside_a_replacement_is_clamped_to_its_start() -> None:
    c = Content("abcdef", spans=[Span(1, 3, "bold")])
    out = apply_edits(c, [Edit(2, 4, "X")])
    assert out.plain == "abXef"
    assert [(s.start, s.end) for s in out.spans] == [(1, 2)]


def test_span_start_inside_a_replacement_is_clamped_to_its_end() -> None:
    c = Content("abcdef", spans=[Span(3, 5, "bold")])
    out = apply_edits(c, [Edit(2, 4, "XY")])
    assert out.plain == "abXYef"
    assert [(s.start, s.end) for s in out.spans] == [(4, 5)]


def test_span_containing_an_edit_still_spans_the_replacement() -> None:
    c = Content("abcdef", spans=[Span(1, 5, "bold")])
    out = apply_edits(c, [Edit(2, 4, "XYZ")])
    assert out.plain == "abXYZef"
    assert [(s.start, s.end) for s in out.spans] == [(1, 6)]


def test_span_ending_at_an_insertion_point_stays_before_it() -> None:
    c = Content("ab", spans=[Span(0, 1, "bold")])
    out = apply_edits(c, [Edit(1, 1, "XYZ")])
    assert out.plain == "aXYZb"
    assert [(s.start, s.end) for s in out.spans] == [(0, 1)]


def test_span_starting_at_an_insertion_point_moves_after_it() -> None:
    c = Content("ab", spans=[Span(1, 2, "bold")])
    out = apply_edits(c, [Edit(1, 1, "XYZ")])
    assert out.plain == "aXYZb"
    assert [(s.start, s.end) for s in out.spans] == [(4, 5)]


def test_kept_range_preserves_spans_over_the_reproduced_text() -> None:
    """``kept`` marks text the replacement reproduces verbatim, so spans survive."""
    content = Content("==a b c==", spans=[Span(4, 5, "bold")])
    out = apply_edits(content, [Edit(0, 9, "a b c", kept=(2, 7))])
    assert out.plain == "a b c"
    assert [(s.start, s.end, str(s.style)) for s in out.spans] == [(2, 3, "bold")]


def test_kept_range_still_clamps_spans_over_removed_delimiters() -> None:
    content = Content("==a b c==", spans=[Span(0, 5, "bold")])
    out = apply_edits(content, [Edit(0, 9, "a b c", kept=(2, 7))])
    assert out.plain == "a b c"
    assert [(s.start, s.end) for s in out.spans] == [(0, 3)]

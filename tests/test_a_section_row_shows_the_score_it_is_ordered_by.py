"""A section row shows the score its file's sections are ordered by, dimmed when it
misses a query word another section holds."""

from __future__ import annotations

import dataclasses

from fnd.query import Hit
from fnd.tui.results_labels import _format_hit_label


def _hit(**changes: object) -> Hit:
    hit = Hit(
        score=5.27,
        parent_id="p",
        path="/notes/course.md",
        kind="md",
        page=0,
        slide=0,
        heading_path="Course > Learning activity",
        title="Course",
        snippet="One learning task.",
    )
    return dataclasses.replace(hit, **changes)  # type: ignore[arg-type]


def _styles(label: object) -> set[str]:
    return {str(span.style) for span in label.spans}  # type: ignore[attr-defined]


def test_the_row_shows_the_ordering_score() -> None:
    label = _format_hit_label(_hit(section_score=16.33), max_score=20.0)
    assert str(label).lstrip().startswith("16.33")


def test_without_a_section_score_the_row_shows_bm25() -> None:
    assert str(_format_hit_label(_hit(), max_score=20.0)).lstrip().startswith("5.27")


def test_a_section_missing_a_word_is_dimmed() -> None:
    assert _styles(_format_hit_label(_hit(misses_a_word=True), max_score=20.0)) == {"dim"}


def test_a_section_holding_every_word_is_not_dimmed() -> None:
    assert "dim" not in _styles(_format_hit_label(_hit(), max_score=20.0))

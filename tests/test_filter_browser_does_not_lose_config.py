"""The filter browser must not silently discard what it cannot display."""

from __future__ import annotations

import datetime as dt

import pytest

from fnd.filters import FilterSpec
from fnd.filters.scan import SourceSample
from fnd.filters.tree_model import apply_selection, selection_for, spec_branches


def _round_trip(spec: FilterSpec, also_select: set[str] | None = None) -> FilterSpec:
    selected, excluded = selection_for(spec, gitignore=True, fndignore=True)
    back, _git, _fnd = apply_selection(spec, selected | (also_select or set()), excluded)
    return back


class TestBoundsTheRadiosCannotShow:
    """The options set a bound; the bound is an arbitrary number or date."""

    @pytest.mark.parametrize(
        "spec",
        [
            FilterSpec(max_size=5_000_000),
            FilterSpec(max_size=1),
            FilterSpec(modified_after=dt.date(2024, 1, 1)),
            FilterSpec(created_after=dt.date(2025, 1, 1)),
            FilterSpec(max_size=7_777, modified_after=dt.date(2023, 6, 30)),
        ],
    )
    def test_an_unlisted_bound_survives_the_tree(self, spec: FilterSpec) -> None:
        assert _round_trip(spec) == spec

    @pytest.mark.parametrize(
        "spec",
        [FilterSpec(max_size=5_000_000), FilterSpec(modified_after=dt.date(2024, 1, 1))],
    )
    def test_an_unrelated_toggle_does_not_delete_it(self, spec: FilterSpec) -> None:
        """Ticking a file type rewrote every radio-backed field from the tree."""
        back = _round_trip(spec, {"kind:pdf"})
        assert back.max_size == spec.max_size
        assert back.modified_after == spec.modified_after

    def test_the_row_says_what_the_bound_is(self) -> None:
        branches = spec_branches(FilterSpec(max_size=5_000_000), None)
        size = next(b for b in branches if b.id == "size")
        labels = [lbl for _id, lbl in size.items]
        assert any("5 MB" in lbl for lbl in labels), labels

    def test_a_window_still_matches_the_day_after_it_was_chosen(self) -> None:
        """A window resolves to an absolute date; two days on it stopped being
        recognised and the next toggle deleted it."""
        spec = FilterSpec(modified_after=dt.date.today() - dt.timedelta(days=32))
        assert _round_trip(spec, {"kind:pdf"}).modified_after == spec.modified_after


class TestKindsTheSampleNeverSaw:
    def test_a_configured_kind_absent_from_the_source_is_still_offered(self) -> None:
        """Otherwise the rule is live but invisible, under a branch that reads
        as unfiltered, and ticking one type silently keeps the others."""
        spec = FilterSpec(kinds=("docx", "pptx"))
        sample = SourceSample(kinds={"md": 4})
        branches = spec_branches(spec, sample)
        kinds = next(b for b in branches if b.id == "kinds")
        offered = {i[0] for g in kinds.groups for i in g.items}
        assert {"kind:docx", "kind:pptx"} <= offered, offered

    def test_and_it_can_be_switched_off(self) -> None:
        spec = FilterSpec(kinds=("docx", "pptx"))
        selected, excluded = selection_for(spec, gitignore=True, fndignore=True)
        back, _g, _f = apply_selection(spec, selected - {"kind:docx"}, excluded)
        assert back.kinds == ("pptx",)

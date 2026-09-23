"""The filter browser must not silently discard what it cannot display."""

from __future__ import annotations

import datetime as dt

import pytest

from fnd.filters import FilterSpec
from fnd.filters.scan import SourceSample
from fnd.filters.tree_model import apply_selection, selection_for, spec_branches


def _offered(spec: FilterSpec, sample: object | None = None) -> set[str]:
    """The kind ids the tree would actually show for ``spec``."""
    from fnd.filters.tree_model import spec_branches

    return {
        item[0]
        for branch in spec_branches(spec, sample)  # type: ignore[arg-type]
        if branch.id == "kinds"
        for group in branch.groups
        for item in group.items
    }


def _round_trip(
    spec: FilterSpec,
    also_select: set[str] | None = None,
    sample: object | None = None,
) -> FilterSpec:
    """As the screen does it, INCLUDING `offered`.

    Passing no offered set left the all-ticked collapse unreachable, so the
    one defect this file exists to catch (a real rule silently discarded)
    could not fire in any test here.
    """
    selected, excluded = selection_for(spec, gitignore=True, fndignore=True)
    back, _git, _fnd = apply_selection(
        spec, selected | (also_select or set()), excluded, _offered(spec, sample)
    )
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


class TestTheCustomRowMeansItsLabel:
    """The tree's labels are built when it rebuilds; a selection is resolved
    when it is made. An id meaning "whatever the spec holds now" would resolve
    a row still reading "Up to 5 MB" to a bound the user has since changed.

    These deliberately do NOT re-derive the selection from the spec: doing so
    reconciles the two and hides the defect.
    """

    def test_reselecting_a_stale_row_gives_what_it_says(self) -> None:
        spec = FilterSpec(max_size=5_000_000)
        selected, excluded = selection_for(spec, gitignore=True, fndignore=True)
        moved, _g, _f = apply_selection(
            spec, {i for i in selected if not i.startswith("size:")} | {"size:10mb"}, excluded
        )
        assert moved.max_size == 10_000_000

        stale = {i for i in selected if not i.startswith("size:")} | {"size:custom:5000000"}
        back, _g, _f = apply_selection(moved, stale, excluded)
        assert back.max_size == 5_000_000

    def test_the_same_holds_for_a_date(self) -> None:
        spec = FilterSpec(modified_after=dt.date(2024, 1, 1))
        selected, excluded = selection_for(spec, gitignore=True, fndignore=True)
        moved, _g, _f = apply_selection(
            spec,
            {i for i in selected if not i.startswith("modified:")} | {"modified:30"},
            excluded,
        )
        assert moved.modified_after == dt.date.today() - dt.timedelta(days=30)

        stale = {i for i in selected if not i.startswith("modified:")} | {
            "modified:custom:2024-01-01"
        }
        back, _g, _f = apply_selection(moved, stale, excluded)
        assert back.modified_after == dt.date(2024, 1, 1)

    def test_the_id_carries_the_value(self) -> None:
        selected, _excluded = selection_for(
            FilterSpec(max_size=5_000_000), gitignore=True, fndignore=True
        )
        assert "size:custom:5000000" in selected


class TestAHomogeneousFolderKeepsItsRule:
    """The defect this file exists to catch: the tree lists what a source
    contains, so on an all-markdown folder a real `kinds = ["md"]` is
    "everything offered" and must not collapse to no rule (deleted by opening
    the screen, with the index widened to match)."""

    def test_the_rule_survives(self) -> None:
        from fnd.filters.scan import SourceSample

        spec = FilterSpec(kinds=("md",))
        sample = SourceSample(kinds={"md": 12}, tags={})
        assert _round_trip(spec, sample=sample).kinds == ("md",)

    def test_the_branch_may_now_claim_every_type(self) -> None:
        """Ticking every VISIBLE kind on a sampled list would collapse to
        "every type" and silently widen; the list is never sampled, so the
        promise is honest, and the collapse test below proves ticking all of
        them is what earns it."""
        from fnd.filters.scan import SourceSample
        from fnd.filters.tree_model import spec_branches

        sample = SourceSample(kinds={"md": 12}, tags={})
        branch = next(
            b for b in spec_branches(FilterSpec(kinds=("md",)), sample) if b.id == "kinds"
        )
        assert branch.full_label == "every type"

    def test_a_complete_offer_still_collapses(self) -> None:
        from fnd.filters.scan import SourceSample
        from fnd.kinds import ALL_KIND_IDS

        every = FilterSpec(kinds=tuple(ALL_KIND_IDS))
        sample = SourceSample(kinds=dict.fromkeys(ALL_KIND_IDS, 1), tags={})
        assert _round_trip(every, sample=sample).kinds == ()

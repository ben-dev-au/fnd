"""The File type legend does not advertise an exclude state it lacks.

`⊘ never index these` is the shared line, and `kinds` is an include-only tuple
in the model: there is no exclude state to reach. The workaround, allow-listing
every other category, silently drops every file type added to the registry
later.
"""

from __future__ import annotations

import pytest

from fnd.filters import FilterSpec
from fnd.filters.scan import SourceSample
from fnd.filters.tree_model import KINDS_LEGEND, LEGEND, spec_branches


def _branch(branch_id: str):
    sample = SourceSample(kinds={"md": 2}, tags={"frontmatter": {"keep": 1}})
    return next(b for b in spec_branches(FilterSpec(), sample) if b.id == branch_id)


def test_the_model_has_no_exclude_state_for_kinds() -> None:
    """The premise. If this gains a field, the legend should change back."""
    assert not hasattr(FilterSpec(), "exclude_kinds")
    assert isinstance(FilterSpec().kinds, tuple)


def test_the_file_types_branch_does_not_advertise_it() -> None:
    legend = _branch("kinds").legend

    assert legend, "it inherited the shared line, which promises ⊘"
    assert "⊘  never index these" not in legend
    assert legend == KINDS_LEGEND


def test_it_names_the_route_that_can() -> None:
    """Saying "you cannot" without saying "here is how" is half an answer."""
    assert "t" in _branch("kinds").legend
    assert "⊘" in _branch("kinds").legend, "the glyph is still explained, as unavailable"


def test_a_branch_that_does_reach_it_keeps_the_shared_line() -> None:
    """The control: tags DO reach ⊘, and must keep saying so."""
    assert not _branch("tags").legend, "no override means the shared line"
    assert "⊘  never index these" in LEGEND


def _all_branch_ids() -> list[str]:
    sample = SourceSample(kinds={"md": 2}, tags={"frontmatter": {"keep": 1}})
    return [b.id for b in spec_branches(FilterSpec(), sample)]


@pytest.mark.parametrize("branch_id", _all_branch_ids())
def test_every_legend_shown_is_true_of_its_branch(branch_id: str) -> None:
    """A branch may only advertise ⊘ where a leaf can hold it."""
    branch = _branch(branch_id)
    shown = branch.legend or LEGEND
    reaches_exclude = branch.mode == "cycle" or any(g.mode == "cycle" for g in branch.groups)

    assert ("never index these" in shown) == reaches_exclude, shown


@pytest.mark.parametrize("branch_id", _all_branch_ids())
def test_no_legend_promises_a_partial_state_a_radio_cannot_hold(branch_id: str) -> None:
    """`◐` means "some of these", which one-row-at-a-time branches cannot reach."""
    branch = _branch(branch_id)
    shown = branch.legend or LEGEND
    holds_many = branch.mode != "radio" or any(g.mode != "radio" for g in branch.groups)

    assert ("◐" in shown) <= holds_many, shown

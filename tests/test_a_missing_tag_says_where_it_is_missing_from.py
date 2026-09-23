"""A tag that IS in the index was filed under "No longer in the index".

The tag catalogue is scoped to the active query, so with one running a
selected tag absent from it is missing from the RESULTS. The branch said
"index", which to a user checking whether their private tags disappeared reads
as confirmation that something left it.
"""

from __future__ import annotations

import pytest

from fnd.tui.scope_panel import _absent, _tags_summary


def test_under_a_query_a_missing_tag_is_missing_from_the_results() -> None:
    """The catalogue was narrowed by the query, so that is what it can speak for."""
    assert _absent(searching=True) == "not in these results"


def test_with_no_query_the_catalogue_covers_the_scope() -> None:
    """The control: unnarrowed, a selected tag that is absent really has gone."""
    assert _absent(searching=False) == "not in the index"


@pytest.mark.parametrize("searching", [True, False])
def test_the_summary_carries_the_same_claim_as_the_branch(searching: bool) -> None:
    """One vocabulary: the count beside the branch and the branch itself must
    not disagree about where a tag went."""
    summary = _tags_summary(1, 1, sources_on=True, n_missing=1, searching=searching)

    assert _absent(searching) in summary, summary

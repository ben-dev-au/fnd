"""`File types (every type)` sat four lines above an expression excluding python.

The pickers own the shapes they can render; anything else stays as typed text,
and the branch went on claiming there was no rule on its dimension. The size
and date branches already say "a rule is typed below" for exactly this; the
two branches a user is most likely to type a rule FOR did not.
"""

from __future__ import annotations

import pytest

from fnd.filters import FilterSpec
from fnd.filters.scan import SourceSample
from fnd.filters.tree_model import spec_branches

_SAMPLE = SourceSample(kinds={"md": 2}, tags={"frontmatter": {"keep": 1}})


def _branch(spec: FilterSpec, branch_id: str):
    return next(b for b in spec_branches(spec, _SAMPLE) if b.id == branch_id)


def test_a_typed_kind_rule_is_admitted() -> None:
    spec = FilterSpec(expression="NOT (file.kind in ['python'])")

    assert _branch(spec, "kinds").elsewhere, "it claimed every type"


def test_a_typed_tag_rule_is_admitted() -> None:
    spec = FilterSpec(expression="NOT ('no_index' in file.tags.all)")

    assert _branch(spec, "tags").elsewhere, "it claimed any tag"


@pytest.mark.parametrize("branch_id", ["kinds", "tags"])
def test_an_unrelated_rule_is_not_claimed(branch_id: str) -> None:
    """The control: a rule about another dimension must not mark this one."""
    spec = FilterSpec(expression="file.size < 100")

    assert not _branch(spec, branch_id).elsewhere, branch_id


@pytest.mark.parametrize("branch_id", ["kinds", "tags"])
def test_no_rule_says_nothing(branch_id: str) -> None:
    assert not _branch(FilterSpec(), branch_id).elsewhere, branch_id


def test_the_size_branch_still_does_its_own() -> None:
    """The mechanism this reuses, unchanged."""
    spec = FilterSpec(expression="file.size < 100")

    assert _branch(spec, "size").elsewhere


def test_unparseable_text_teaches_a_branch_nothing() -> None:
    """The editor reports the syntax error; a branch must not guess from it."""
    spec = FilterSpec(expression="file.kind in [")

    assert not _branch(spec, "kinds").elsewhere

"""Opening the text view and saving it unchanged changes nothing.

The browser offers the tree "or as one expression if you prefer". Pressing `t`
and then saving with no edit moved a rule from ``frontmatter`` to
``expression``; the same text, a different field, and a different index: a
frontmatter rule is skipped for a file that has none, while the same text as
an expression strict-nulls that file out. Eleven chunks against ten.
"""

from __future__ import annotations

import pytest

from fnd.filters import FilterSpec
from fnd.filters.text_form import parse, render

_SPECS = [
    FilterSpec(frontmatter="Course == 'DPwC'"),
    FilterSpec(frontmatter="status == 'done' AND draft == false"),
    FilterSpec(frontmatter="Course == 'DPwC'", kinds=("md", "pdf")),
    FilterSpec(frontmatter="file.name ~~ '*draft*'"),
    FilterSpec(frontmatter="Course == 'DPwC' AND file.size > 100"),
    FilterSpec(expression="file.size > 100"),
    FilterSpec(frontmatter="Course == 'X'", expression="file.size > 100"),
    FilterSpec(frontmatter="Course == 'X'", exclude_tags={"frontmatter": ("no_index",)}),
]


@pytest.mark.parametrize("spec", _SPECS, ids=lambda s: f"{s.frontmatter[:20]}|{s.expression[:16]}")
def test_the_text_view_is_a_view(spec: FilterSpec) -> None:
    back = parse(render(spec))
    assert (back.frontmatter, back.expression) == (spec.frontmatter, spec.expression)


def test_a_rule_naming_a_file_field_is_not_a_frontmatter_rule() -> None:
    """It is moved when the user types it, not later on an unrelated key."""
    spec = FilterSpec(frontmatter="file.name ~~ '*draft*'")
    assert spec.frontmatter == ""
    assert spec.expression == "file.name ~~ '*draft*'"


def test_a_mixed_rule_keeps_both_halves() -> None:
    spec = FilterSpec(frontmatter="Course == 'DPwC' AND file.size > 100")
    assert "Course" in spec.frontmatter
    assert "file.size" not in spec.frontmatter
    assert "file.size" in spec.expression


def test_a_real_frontmatter_rule_stays_where_it_is() -> None:
    """The control: canonicalising must not evict the rules that belong."""
    spec = FilterSpec(frontmatter="status == 'done' AND draft == false")
    assert spec.frontmatter == "status == 'done' AND draft == false"
    assert spec.expression == ""


def test_an_expression_of_only_frontmatter_fields_still_moves_the_other_way() -> None:
    """The rescue that already existed, kept: left in `expression` such a rule
    is evaluated against every file and drops every PDF."""
    spec = FilterSpec(expression="Course == 'DPwC'")
    assert spec.frontmatter == "Course == 'DPwC'"
    assert spec.expression == ""

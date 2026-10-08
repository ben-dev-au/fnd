"""``fnd.query_filters._tokenize_top_level``: a bracketed run ends at its own
closer, so a proximity ``{N}`` inside a group never ends the group."""

from __future__ import annotations

import pytest

from fnd.query_filters import _tokenize_top_level, extract_filters
from fnd.schema import build_schema


@pytest.mark.parametrize(
    ("query", "tokens"),
    [
        ("({2} a b kind:pdf)", ["({2} a b kind:pdf)"]),
        ("page:{1 TO 9} x", ["page:{1 TO 9}", "x"]),
        ("page:[1 TO 9} x", ["page:[1 TO 9}", "x"]),
        ("page:{1 TO 9] x", ["page:{1 TO 9]", "x"]),
        ("{6} cross entropy", ["{6}", "cross", "entropy"]),
        ("(a OR b) kind:pdf", ["(a OR b)", "kind:pdf"]),
    ],
)
def test_a_run_ends_at_its_own_closer(query: str, tokens: list[str]) -> None:
    assert _tokenize_top_level(query) == tokens


def test_a_filter_inside_a_proximity_group_is_not_lifted() -> None:
    result = extract_filters("({2} cross entropy kind:pdf)", build_schema())
    assert result.filters == []
    assert result.content == "({2} cross entropy kind:pdf)"

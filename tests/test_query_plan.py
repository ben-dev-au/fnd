"""QueryPlan: the single raw-text -> validated-query path shared by CLI + TUI."""

from __future__ import annotations

import pytest

from fnd.query_errors import QuerySyntaxError, QueryTooLargeError
from fnd.query_plan import QueryPlan


def test_plain_query() -> None:
    plan = QueryPlan.from_user_text("cross entropy")
    assert plan.lexical == "cross entropy"
    assert plan.metadata_filter is None


def test_splits_metadata_filter() -> None:
    plan = QueryPlan.from_user_text('mitm [Course == "Security"]')
    assert plan.lexical == "mitm"
    assert plan.metadata_filter == 'Course == "Security"'


def test_valid_proximity_passes_lexical_unexpanded() -> None:
    # Plan keeps the human lexical; DSL expansion happens downstream.
    plan = QueryPlan.from_user_text("{60} buffer overflow")
    assert plan.lexical == "{60} buffer overflow"
    assert plan.metadata_filter is None


def test_malformed_proximity_raises() -> None:
    with pytest.raises(QuerySyntaxError):
        QueryPlan.from_user_text("{60}")


def test_unbalanced_bracket_raises_syntax_error() -> None:
    with pytest.raises(QuerySyntaxError):
        QueryPlan.from_user_text("foo [Course == ")


def test_oversized_boolean_query_raises() -> None:
    with pytest.raises(QueryTooLargeError):
        QueryPlan.from_user_text(" OR ".join(["a"] * 100))

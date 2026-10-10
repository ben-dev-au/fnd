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
    plan = QueryPlan.from_user_text('deadline [Project == "Payments"]')
    assert plan.lexical == "deadline"
    assert plan.metadata_filter == 'Project == "Payments"'


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
        QueryPlan.from_user_text("foo [Project == ")


def test_oversized_boolean_query_raises() -> None:
    with pytest.raises(QueryTooLargeError):
        QueryPlan.from_user_text(" OR ".join(["a"] * 100))


def test_the_plan_holds_the_canonical_text() -> None:
    raw = "cafe\N{COMBINING ACUTE ACCENT} cro\N{ZERO WIDTH SPACE}ss \N{LATIN SMALL LIGATURE FI}le"
    assert (
        QueryPlan.from_user_text(raw).lexical == "caf\N{LATIN SMALL LETTER E WITH ACUTE} cross file"
    )


@pytest.mark.parametrize(
    ("typed", "means"),
    [
        ("\N{LEFT DOUBLE QUOTATION MARK}a b\N{RIGHT DOUBLE QUOTATION MARK}", '"a b"'),
        ("\N{DOUBLE LOW-9 QUOTATION MARK}a b\N{LEFT DOUBLE QUOTATION MARK}", '"a b"'),
        ("don\N{RIGHT SINGLE QUOTATION MARK}t", "don't"),
        ("\N{LEFT SINGLE QUOTATION MARK}a b\N{RIGHT SINGLE QUOTATION MARK}", "'a b'"),
    ],
)
def test_typographic_quotes_are_the_ascii_syntax(typed: str, means: str) -> None:
    assert QueryPlan.from_user_text(typed).lexical == means


@pytest.mark.parametrize("query", ['"unbalanced', 'a "b c', '"a" "b'])
def test_an_unclosed_double_quote_is_refused(query: str) -> None:
    with pytest.raises(QuerySyntaxError, match="quote"):
        QueryPlan.from_user_text(query)


@pytest.mark.parametrize("query", ["'90s music", "rock 'n' roll", "don't"])
def test_an_apostrophe_is_never_an_unclosed_quote(query: str) -> None:
    assert QueryPlan.from_user_text(query).lexical == query


@pytest.mark.parametrize("query", ["(foo", "foo)", "a AND", "OR b", "a NOT", "(a OR b"])
def test_unbalanced_structure_is_refused(query: str) -> None:
    with pytest.raises(QuerySyntaxError):
        QueryPlan.from_user_text(query)


@pytest.mark.parametrize(
    "query",
    [
        "(a OR b) AND c",
        "NOT a",
        "title:(a OR b)",
        '"(x"',
        "/a(b/",
        "a -b",
        "a [kind = pdf]",
        "/crypto.*/ AND hash",
        '"a b" OR c',
    ],
)
def test_balanced_structure_passes(query: str) -> None:
    QueryPlan.from_user_text(query)

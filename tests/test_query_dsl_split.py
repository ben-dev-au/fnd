"""Phase 5.5e-2: extract a single inline [metadata filter] clause from a query."""

from __future__ import annotations

import pytest

from fnd.query_dsl import split_metadata_filter


def test_no_brackets_returns_query_unchanged() -> None:
    assert split_metadata_filter("strategy pattern") == ("strategy pattern", None)


def test_brackets_at_start() -> None:
    q, m = split_metadata_filter("[Course == 'DPwC'] strategy pattern")
    assert q == "strategy pattern"
    assert m == "Course == 'DPwC'"


def test_brackets_at_end() -> None:
    q, m = split_metadata_filter("strategy pattern [Course == 'DPwC']")
    assert q == "strategy pattern"
    assert m == "Course == 'DPwC'"


def test_brackets_in_middle() -> None:
    q, m = split_metadata_filter("foo [Course == 'DPwC'] bar")
    assert q == "foo bar"
    assert m == "Course == 'DPwC'"


def test_brackets_inside_quoted_phrase_left_alone() -> None:
    """A phrase in the user's lexical query may legitimately contain
    square brackets (e.g. a code listing). Those must not be extracted."""
    q, m = split_metadata_filter('"foo [bar]" baz')
    assert q == '"foo [bar]" baz'
    assert m is None


def test_multiple_brackets_raises() -> None:
    """Only one filter clause per query in v1; users compose with AND/OR
    inside the single block."""
    with pytest.raises(ValueError, match="only one"):
        split_metadata_filter("[a == 1] foo [b == 2]")


def test_empty_brackets_returns_none() -> None:
    """An empty [] is a no-op, not an empty filter — surface as None so
    the search runs without metadata filtering."""
    q, m = split_metadata_filter("foo []")
    assert q == "foo"
    assert m is None


def test_whitespace_around_extracted_clause_collapsed() -> None:
    """Removing the bracketed clause shouldn't leave double-spaces in the
    lexical query."""
    q, _m = split_metadata_filter("foo  [a == 1]  bar")
    # Single space between foo and bar.
    assert q == "foo bar"


def test_unclosed_bracket_raises() -> None:
    with pytest.raises(ValueError, match=r"unclosed|unterminated"):
        split_metadata_filter("foo [a == 1")

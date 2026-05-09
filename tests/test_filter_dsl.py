"""Phase 5.5e-1: predicate DSL parser + evaluator."""

from __future__ import annotations

import datetime as dt

import pytest

from acorn.filter_dsl import And, Compare, FilterError, In, Not, Or, TokenKind, parse, tokenize


def _kinds(text: str) -> list[TokenKind]:
    return [t.kind for t in tokenize(text)]


def test_tokenize_simple_equality() -> None:
    toks = tokenize("Course == 'DPwC'")
    assert [(t.kind, t.value) for t in toks] == [
        (TokenKind.IDENT, "Course"),
        (TokenKind.OP, "=="),
        (TokenKind.STRING, "DPwC"),
        (TokenKind.EOF, ""),
    ]


def test_tokenize_keywords_case_insensitive() -> None:
    assert _kinds("a AND b or NOT c") == [
        TokenKind.IDENT,
        TokenKind.AND,
        TokenKind.IDENT,
        TokenKind.OR,
        TokenKind.NOT,
        TokenKind.IDENT,
        TokenKind.EOF,
    ]


def test_tokenize_all_operators() -> None:
    toks = tokenize("== != < > <= >= ~~")
    assert [t.value for t in toks if t.kind == TokenKind.OP] == [
        "==",
        "!=",
        "<",
        ">",
        "<=",
        ">=",
        "~~",
    ]


def test_tokenize_numbers_and_dates() -> None:
    toks = tokenize("priority >= 3 AND due <= 2026-06-01")
    values = [t.value for t in toks if t.kind in (TokenKind.NUMBER, TokenKind.DATE)]
    assert values == [3, dt.date(2026, 6, 1)]


def test_tokenize_in_and_not_in() -> None:
    assert _kinds("'x' in tags") == [
        TokenKind.STRING,
        TokenKind.IN,
        TokenKind.IDENT,
        TokenKind.EOF,
    ]
    assert _kinds("'x' not in tags") == [
        TokenKind.STRING,
        TokenKind.NOT_IN,
        TokenKind.IDENT,
        TokenKind.EOF,
    ]


def test_tokenize_quoted_identifier() -> None:
    toks = tokenize('"due date" <= 2026-06-01')
    assert toks[0].kind == TokenKind.IDENT
    assert toks[0].value == "due date"


def test_tokenize_unterminated_string_raises_with_column() -> None:
    with pytest.raises(FilterError) as exc:
        tokenize("Course == 'DPwC")
    assert "unterminated" in exc.value.message.lower()
    assert exc.value.column == 11  # column of the opening quote (1-based)


def test_parse_simple_compare() -> None:
    tree = parse("Course == 'DPwC'")
    assert tree == Compare("Course", "==", "DPwC")


def test_parse_and_or_precedence() -> None:
    """AND binds tighter than OR (matches typical predicate languages)."""
    tree = parse("a == 1 OR b == 2 AND c == 3")
    # Expected: a == 1 OR (b == 2 AND c == 3)
    assert tree == Or(
        Compare("a", "==", 1),
        And(Compare("b", "==", 2), Compare("c", "==", 3)),
    )


def test_parse_parens_override_precedence() -> None:
    tree = parse("(a == 1 OR b == 2) AND c == 3")
    assert tree == And(
        Or(Compare("a", "==", 1), Compare("b", "==", 2)),
        Compare("c", "==", 3),
    )


def test_parse_not() -> None:
    tree = parse("NOT a == 1")
    assert tree == Not(Compare("a", "==", 1))


def test_parse_in_membership() -> None:
    tree = parse("'course' in tags")
    assert tree == In("course", "tags", negated=False)


def test_parse_not_in() -> None:
    tree = parse("'archived' not in tags")
    assert tree == In("archived", "tags", negated=True)


def test_parse_quoted_identifier_with_space() -> None:
    tree = parse('"due date" <= 2026-06-01')
    assert tree == Compare("due date", "<=", dt.date(2026, 6, 1))


def test_parse_empty_raises() -> None:
    with pytest.raises(FilterError, match=r"empty|expected"):
        parse("")


def test_parse_dangling_operator_raises_with_column() -> None:
    with pytest.raises(FilterError) as exc:
        parse("Course ==")
    assert exc.value.column >= 9


def test_parse_unmatched_paren_raises() -> None:
    with pytest.raises(FilterError, match=r"paren|expected"):
        parse("(a == 1")

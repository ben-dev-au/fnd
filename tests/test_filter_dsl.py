"""Phase 5.5e-1: predicate DSL parser + evaluator."""

from __future__ import annotations

import datetime as dt

import pytest

from acorn.filter_dsl import FilterError, TokenKind, tokenize


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

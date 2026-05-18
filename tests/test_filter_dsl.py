"""Phase 5.5e-1: predicate DSL parser + evaluator."""

from __future__ import annotations

import datetime as dt

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fnd.filter_dsl import (
    And,
    Compare,
    FilterError,
    In,
    Not,
    Or,
    TokenKind,
    compile_filter,
    parse,
    parse_or_error,
    tokenize,
)


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


def test_eval_equality_match() -> None:
    pred = compile_filter("Course == 'DPwC'")
    assert pred({"Course": "DPwC"}) is True
    assert pred({"Course": "Other"}) is False


def test_eval_inequality() -> None:
    pred = compile_filter("status != 'archived'")
    assert pred({"status": "active"}) is True
    assert pred({"status": "archived"}) is False


def test_eval_missing_field_strict_null() -> None:
    """Per spec: missing field treats the predicate as False — even for !=
    and even for `not in`. The user opted into strict null."""
    pred_eq = compile_filter("Course == 'DPwC'")
    pred_neq = compile_filter("Course != 'DPwC'")
    pred_in = compile_filter("'x' in tags")
    pred_not_in = compile_filter("'x' not in tags")
    empty: dict[str, object] = {}
    assert pred_eq(empty) is False
    assert pred_neq(empty) is False
    assert pred_in(empty) is False
    assert pred_not_in(empty) is False


def test_eval_numeric_compare() -> None:
    pred = compile_filter("priority >= 3")
    assert pred({"priority": 3}) is True
    assert pred({"priority": 5}) is True
    assert pred({"priority": 2}) is False


def test_eval_date_compare() -> None:
    pred = compile_filter("due <= 2026-06-01")
    assert pred({"due": dt.date(2026, 5, 30)}) is True
    assert pred({"due": dt.date(2026, 6, 2)}) is False


def test_eval_type_mismatch_returns_false() -> None:
    """String < number doesn't crash; it's just False."""
    pred = compile_filter("Course < 5")
    assert pred({"Course": "DPwC"}) is False


def test_eval_glob_match() -> None:
    pred = compile_filter("Course ~~ 'Design *'")
    assert pred({"Course": "Design Patterns"}) is True
    assert pred({"Course": "Algorithms"}) is False


def test_eval_in_list() -> None:
    pred = compile_filter("'course' in tags")
    assert pred({"tags": ["course", "active"]}) is True
    assert pred({"tags": ["something", "else"]}) is False


def test_eval_and_or_not() -> None:
    pred = compile_filter("Course == 'DPwC' AND status != 'archived' AND 'active' in tags")
    assert pred({"Course": "DPwC", "status": "active", "tags": ["active"]}) is True
    assert pred({"Course": "DPwC", "status": "archived", "tags": ["active"]}) is False


def test_parse_or_error_returns_predicate_for_valid() -> None:
    pred, err = parse_or_error("Course == 'DPwC'")
    assert err is None
    assert pred is not None
    assert pred({"Course": "DPwC"}) is True


def test_parse_or_error_returns_error_for_invalid() -> None:
    pred, err = parse_or_error("Course ==")
    assert pred is None
    assert err is not None
    assert err.column >= 9


def test_compile_filter_invalid_raises() -> None:
    with pytest.raises(FilterError):
        compile_filter("not valid syntax!")


def test_eval_bool_and_int_are_not_interchangeable() -> None:
    """Per YAML semantics: ``true`` and ``1`` are distinct values, so an
    equality between a bool actual and a non-bool literal (or vice versa)
    must return False, not silently match via Python's ``True == 1``."""
    pred_eq_int = compile_filter("archived == 1")
    pred_neq_int = compile_filter("archived != 1")
    pred_eq_true = compile_filter("active == true")
    # Bool actual, int literal: must NOT match.
    assert pred_eq_int({"archived": True}) is False
    # Bool actual, int literal, !=: must be True (they're not equal).
    assert pred_neq_int({"archived": True}) is True
    # Same-type bool == bool still works.
    assert pred_eq_true({"active": True}) is True
    assert pred_eq_true({"active": False}) is False


@st.composite
def _fields_and_values(draw: st.DrawFn) -> tuple[str, object]:
    field = draw(st.sampled_from(["a", "b", "c", "Course", "status"]))
    value = draw(
        st.one_of(
            st.text(min_size=1, max_size=10).filter(lambda s: "'" not in s and '"' not in s),
            st.integers(min_value=0, max_value=100),
        )
    )
    return field, value


@given(_fields_and_values())
def test_property_equality_then_inequality_partition(
    sample: tuple[str, object],
) -> None:
    """For any field/value, eq(fm) XOR neq(fm) is True when the field is
    present (strict null exempts the missing-field case)."""
    field, value = sample
    if isinstance(value, str):
        lit = f"'{value}'"
    else:
        lit = str(value)
    pred_eq = compile_filter(f"{field} == {lit}")
    pred_neq = compile_filter(f"{field} != {lit}")
    fm = {field: value}
    assert pred_eq(fm) ^ pred_neq(fm) or (pred_eq(fm) is False and pred_neq(fm) is False)

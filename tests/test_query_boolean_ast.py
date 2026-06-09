"""Parser-level contract for the boolean query AST (:mod:`fnd.query_ast`).

The end-to-end search semantics live in test_query_acceptance.py; this pins the
*structure* the parser produces so operator precedence, grouping, prefix/boost
handling, and leaf classification can't drift. Pure Python — no index.
"""

from __future__ import annotations

from fnd.query_ast import (
    And,
    Boosted,
    Fuzzy,
    Not,
    Or,
    Phrase,
    Regex,
    Required,
    Term,
    Wildcard,
    parse_query_ast,
)


# ── leaf classification ──────────────────────────────────────────────
def test_plain_term() -> None:
    assert parse_query_ast("entropy") == Term("entropy")


def test_trailing_wildcard_is_prefix() -> None:
    node = parse_query_ast("crypto*")
    assert node == Wildcard("crypto*", prefix="crypto")


def test_leading_and_infix_wildcards_are_globs() -> None:
    assert parse_query_ast("*tion") == Wildcard("*tion", prefix=None)
    assert parse_query_ast("col?r") == Wildcard("col?r", prefix=None)
    assert parse_query_ast("cr*to") == Wildcard("cr*to", prefix=None)


def test_fuzzy_auto_vs_explicit() -> None:
    assert parse_query_ast("proto~") == Fuzzy("proto", distance=None)
    assert parse_query_ast("proto~2") == Fuzzy("proto", distance=2)


def test_regex_keeps_inner_parens_literal() -> None:
    # The ``(`` inside a regex literal must NOT open a grouping node.
    assert parse_query_ast("/crypt(o|id)/") == Regex("crypt(o|id)")


def test_phrase_with_and_without_slop() -> None:
    assert parse_query_ast('"buffer overflow"') == Phrase("buffer overflow", slop=0)
    assert parse_query_ast('"buffer overflow"~3') == Phrase("buffer overflow", slop=3)


# ── boolean structure & precedence ───────────────────────────────────
def test_adjacency_is_implicit_or() -> None:
    assert parse_query_ast("a b c") == Or((Term("a"), Term("b"), Term("c")))


def test_explicit_and() -> None:
    assert parse_query_ast("a AND b") == And((Term("a"), Term("b")))


def test_and_binds_tighter_than_or() -> None:
    # ``a OR b AND c`` → ``a OR (b AND c)``
    assert parse_query_ast("a OR b AND c") == Or((Term("a"), And((Term("b"), Term("c")))))


def test_parenthesised_grouping_overrides_precedence() -> None:
    assert parse_query_ast("(a OR b) AND c") == And((Or((Term("a"), Term("b"))), Term("c")))


def test_nested_groups() -> None:
    assert parse_query_ast("(a AND b) OR (c AND d)") == Or(
        (And((Term("a"), Term("b"))), And((Term("c"), Term("d"))))
    )


def test_wildcard_inside_and() -> None:
    # The regression that motivated the compiler: a wildcard leaf under AND.
    assert parse_query_ast("crypto* AND defence") == And(
        (Wildcard("crypto*", prefix="crypto"), Term("defence"))
    )


# ── prefixes, negation, boost ────────────────────────────────────────
def test_required_and_prohibited_prefixes() -> None:
    assert parse_query_ast("+cross -loss") == Or((Required(Term("cross")), Not(Term("loss"))))


def test_not_keyword() -> None:
    assert parse_query_ast("a NOT b") == Or((Term("a"), Not(Term("b"))))


def test_prohibited_prefix_on_phrase() -> None:
    assert parse_query_ast('-"a b"') == Not(Phrase("a b", slop=0))


def test_term_boost() -> None:
    assert parse_query_ast("foo^2") == Boosted(Term("foo"), 2.0)


def test_group_boost() -> None:
    assert parse_query_ast("(a OR b)^3") == Boosted(Or((Term("a"), Term("b"))), 3.0)


def test_wildcard_with_boost() -> None:
    assert parse_query_ast("crypto*^2") == Boosted(Wildcard("crypto*", prefix="crypto"), 2.0)


# ── edges ────────────────────────────────────────────────────────────
def test_empty_query_is_none() -> None:
    assert parse_query_ast("") is None
    assert parse_query_ast("   ") is None


def test_single_child_groups_unwrap() -> None:
    # A lone term in parens collapses to the term — no needless wrapper node.
    assert parse_query_ast("(entropy)") == Term("entropy")

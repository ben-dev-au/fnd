"""Grammar additions backing fact-namespaced filters: dotted idents, list literals."""

from __future__ import annotations

import datetime as dt

import pytest

from fnd.filter_dsl import (
    Compare,
    FieldIn,
    FilterError,
    In,
    TokenKind,
    compile_filter,
    parse,
    tokenize,
)


def _kinds(text: str) -> list[TokenKind]:
    return [t.kind for t in tokenize(text)]


class TestDottedIdents:
    def test_dotted_ident_is_one_token(self) -> None:
        assert parse("file.kind == 'pdf'") == Compare("file.kind", "==", "pdf")

    def test_deeply_dotted_ident(self) -> None:
        assert parse("file.tags.os == 'x'") == Compare("file.tags.os", "==", "x")

    def test_float_literal_unaffected(self) -> None:
        assert _kinds("a > 1.5")[2] is TokenKind.NUMBER
        assert parse("a > 1.5") == Compare("a", ">", 1.5)

    def test_digit_separators_are_accepted(self) -> None:
        """TOML writes 50_000_000, so an expression copied from the config
        must parse the same number."""
        assert parse("file.size < 50_000_000") == Compare("file.size", "<", 50_000_000)
        assert compile_filter("file.size < 50_000_000")({"file.size": 10}) is True

    @pytest.mark.parametrize("text", ["a < 1__0", "a < 1_", "a < _1"])
    def test_malformed_separators_are_rejected(self, text: str) -> None:
        """Doubled or trailing separators are a parse error, as in TOML."""
        with pytest.raises(FilterError):
            compile_filter(text)

    def test_a_separator_inside_a_float_works(self) -> None:
        assert parse("a < 1_000.5") == Compare("a", "<", 1000.5)

    def test_a_leading_underscore_is_still_an_identifier(self) -> None:
        assert parse("_key == 1") == Compare("_key", "==", 1)

    def test_iso_date_unaffected(self) -> None:
        assert parse("a < 2026-01-01") == Compare("a", "<", dt.date(2026, 1, 1))

    def test_frontmatter_key_still_bare(self) -> None:
        assert parse("Course == 'DPwC'") == Compare("Course", "==", "DPwC")


class TestFieldInList:
    def test_membership(self) -> None:
        pred = compile_filter("file.kind in ['pdf','md']")
        assert pred({"file.kind": "pdf"}) is True
        assert pred({"file.kind": "txt"}) is False

    def test_negated(self) -> None:
        pred = compile_filter("file.kind not in ['pdf']")
        assert pred({"file.kind": "md"}) is True
        assert pred({"file.kind": "pdf"}) is False

    def test_missing_field_is_strict_null_even_when_negated(self) -> None:
        assert compile_filter("file.kind not in ['pdf']")({}) is False

    def test_list_valued_field_does_not_match(self) -> None:
        """A list field belongs on the ``'x' in tags`` form, not this one."""
        assert compile_filter("tags in ['a']")({"tags": ["a"]}) is False

    def test_ast_shape_is_distinct_from_in_node(self) -> None:
        assert parse("file.kind in ['pdf']") == FieldIn("file.kind", ("pdf",), negated=False)
        assert parse("'pdf' in kinds") == In("pdf", "kinds", negated=False)

    def test_bool_int_strictness_matches_equality(self) -> None:
        assert compile_filter("a in [1]")({"a": True}) is False
        assert compile_filter("a == 1")({"a": True}) is False

    def test_mixed_value_types(self) -> None:
        pred = compile_filter("a in [1, 'two', 2026-01-01]")
        assert pred({"a": "two"}) is True
        assert pred({"a": dt.date(2026, 1, 1)}) is True
        assert pred({"a": 3}) is False

    @pytest.mark.parametrize(
        "text",
        ["a in []", "a in [1,]", "a in [1", "a in 1]", "a in ['x' 'y']"],
    )
    def test_malformed_lists_raise_with_a_column(self, text: str) -> None:
        with pytest.raises(FilterError) as exc:
            compile_filter(text)
        assert exc.value.column >= 1


class TestSetContainers:
    def test_frozenset_membership_works(self) -> None:
        """Every tag API returns frozenset; a list-only gate made this False."""
        assert compile_filter("'x' in tags")({"tags": frozenset({"x"})}) is True

    def test_list_membership_unchanged(self) -> None:
        assert compile_filter("'x' in tags")({"tags": ["x"]}) is True

    def test_scalar_container_still_rejected(self) -> None:
        assert compile_filter("'x' in tags")({"tags": "x"}) is False

"""Phase 4: query-DSL pre-pass unit + property tests."""

from __future__ import annotations

import datetime as dt

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fnd import query_dsl

# ── Collection shorthand ─────────────────────────────────────────────────


def test_c_shorthand_single() -> None:
    assert query_dsl.preprocess("c:papers susy") == 'collection:"papers" susy'


def test_c_shorthand_multi() -> None:
    out = query_dsl.preprocess("c:papers,notes susy")
    assert out == '(collection:"papers" OR collection:"notes") susy'


def test_c_shorthand_inside_compound() -> None:
    # The pre-pass touches `c:foo` regardless of surrounding context.
    out = query_dsl.preprocess("c:papers AND quark")
    assert out == 'collection:"papers" AND quark'


def test_c_shorthand_double_quoted_name_with_spaces() -> None:
    out = query_dsl.preprocess('c:"Soft Eng Textbooks" tdd')
    assert out == 'collection:"Soft Eng Textbooks" tdd'


def test_c_shorthand_single_quoted_name_with_spaces() -> None:
    out = query_dsl.preprocess("c:'Soft Eng Textbooks' tdd")
    assert out == 'collection:"Soft Eng Textbooks" tdd'


def test_c_shorthand_mixed_quoted_and_bare_list() -> None:
    out = query_dsl.preprocess('c:papers,"Soft Eng",notes tdd')
    assert out == '(collection:"papers" OR collection:"Soft Eng" OR collection:"notes") tdd'


def test_c_shorthand_quoted_list_only() -> None:
    out = query_dsl.preprocess('c:"a b","c d" tdd')
    assert out == '(collection:"a b" OR collection:"c d") tdd'


# ── Date tokens ──────────────────────────────────────────────────────────


def test_mtime_week_token(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed = int(dt.datetime(2026, 5, 8, tzinfo=dt.UTC).timestamp())
    monkeypatch.setattr(query_dsl, "_now_ts", lambda: fixed)
    out = query_dsl.preprocess("mtime:week supersymmetry")
    expected_low = fixed - 7 * 86_400
    assert out == f"mtime:[{expected_low} TO {query_dsl.FAR_FUTURE}] supersymmetry"


def test_mtime_today_token(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed = int(dt.datetime(2026, 5, 8, tzinfo=dt.UTC).timestamp())
    monkeypatch.setattr(query_dsl, "_now_ts", lambda: fixed)
    out = query_dsl.preprocess("mtime:today notes")
    assert out == f"mtime:[{fixed} TO {query_dsl.FAR_FUTURE}] notes"


# ── Numeric comparison ───────────────────────────────────────────────────


def test_slide_greater_than() -> None:
    out = query_dsl.preprocess("slide:>5 quark")
    assert out == f"slide:[6 TO {query_dsl.FAR_FUTURE}] quark"


def test_slide_less_or_equal() -> None:
    out = query_dsl.preprocess("slide:<=5")
    assert out == f"slide:[{query_dsl.FAR_PAST} TO 5]"


def test_page_range_passthrough() -> None:
    """Bracket form is Tantivy-native — leave it alone."""
    assert query_dsl.preprocess("page:[10 TO 50]") == "page:[10 TO 50]"


def test_mtime_iso_compare() -> None:
    out = query_dsl.preprocess("mtime:>2024-01-01")
    expected = int(dt.datetime(2024, 1, 1, tzinfo=dt.UTC).timestamp())
    assert out == f"mtime:[{expected + 1} TO {query_dsl.FAR_FUTURE}]"


def test_mtime_year_compare() -> None:
    out = query_dsl.preprocess("mtime:>2024")
    expected = int(dt.datetime(2024, 1, 1, tzinfo=dt.UTC).timestamp())
    assert out == f"mtime:[{expected + 1} TO {query_dsl.FAR_FUTURE}]"


# ── Proximity aliases ────────────────────────────────────────────────────


def test_brace_proximity() -> None:
    assert query_dsl.preprocess("{5} foo bar baz") == '"foo bar baz"~5'


def test_brace_proximity_with_quotes() -> None:
    assert query_dsl.preprocess('{3} "foo bar"') == '"foo bar"~3'


def test_brace_proximity_no_space() -> None:
    # Missing space after the brace is a typo we recover from.
    assert query_dsl.preprocess("{5}foo bar") == '"foo bar"~5'


def test_brace_proximity_stops_at_operator() -> None:
    # {N} binds to the leading word run; the operator clause stays outside.
    assert query_dsl.preprocess("{5} foo bar AND quark") == '"foo bar"~5 AND quark'


def test_brace_proximity_stops_at_field_qualifier() -> None:
    out = query_dsl.preprocess("{10} buffer overflow exploit kind:pdf")
    assert out == '"buffer overflow exploit"~10 kind:pdf'


def test_brace_proximity_stops_at_paren() -> None:
    assert query_dsl.preprocess("{5} foo (bar baz)") == '"foo"~5 (bar baz)'


def test_brace_proximity_does_not_swallow_following_brace() -> None:
    # A ``{N}`` must not be consumed as the run word of a preceding brace, which
    # would leave a brace inside quotes that the next pre-pass re-expands. The
    # malformed ``{0}{0}`` stays put (for check_proximity to flag) and preprocess
    # is idempotent.
    out = query_dsl.preprocess("{0}{0}")
    assert out == "{0}{0}"
    assert query_dsl.preprocess(out) == out


def test_near_alias() -> None:
    assert query_dsl.preprocess("foo NEAR/5 bar") == '"foo bar"~5'


# ── Proximity validation (residual braces are user errors) ────────────────


@pytest.mark.parametrize("bad", ["{60}", "{abc} foo", "{} foo", "{-5} foo bar", "{60} kind:pdf"])
def test_check_proximity_rejects_malformed(bad: str) -> None:
    from fnd.query_errors import QuerySyntaxError

    with pytest.raises(QuerySyntaxError):
        query_dsl.check_proximity(query_dsl.preprocess(bad))


@pytest.mark.parametrize(
    "ok",
    [
        '"foo bar"~5',  # expanded proximity — no residual brace
        "page:[10 TO 50]",
        "page:{10 TO 50}",  # Tantivy exclusive range — not a proximity attempt
        "plain words only",
    ],
)
def test_check_proximity_accepts_valid(ok: str) -> None:
    query_dsl.check_proximity(query_dsl.preprocess(ok))  # must not raise


# ── Native syntax round-trips unchanged ──────────────────────────────────


@pytest.mark.parametrize(
    "q",
    [
        "quark gluon",
        '"quark gluon"',
        '"quark gluon"~5',
        "quark OR gluon",
        "quark AND NOT gluon",
        "quark -gluon",
        "+quark gluon",
        "colour~1",
        "comput*",
        "kind:pdf supersymmetry",
        "path:Papers/2024 susy",
        'title:"final draft"',
        "page:[10 TO 50]",
        "(quark OR gluon) AND kind:pdf -path:drafts/",
    ],
)
def test_native_syntax_unchanged(q: str) -> None:
    assert query_dsl.preprocess(q) == q


# ── Property test ─────────────────────────────────────────────────────────


@given(st.text(alphabet=st.characters(blacklist_categories=("Cs", "Cc")), min_size=0, max_size=80))
def test_preprocess_idempotent_on_arbitrary_text(text: str) -> None:
    """preprocess should be a fixed point: applying it twice == once.

    Important so live `:set` updates that re-run the pre-pass don't compound."""
    once = query_dsl.preprocess(text)
    twice = query_dsl.preprocess(once)
    assert once == twice

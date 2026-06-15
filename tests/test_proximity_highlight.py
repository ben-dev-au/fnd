"""Proximity-constrained highlighting: only occurrences inside a qualifying
co-occurrence window render at full strength; the rest are dimmed."""

from __future__ import annotations

from rich.text import Text
from textual.content import Span

from fnd.matching import MatchSpec, _stem, proximity_qualifying_indices


def _stems(*words: str) -> tuple[str, ...]:
    return tuple(_stem(w) for w in words)


def _tokens(index_to_stem: dict[int, str], length: int) -> list[str]:
    """Build a stems-by-token list of ``length`` filler tokens with the given
    stems planted at specific indices."""
    out = ["x"] * length
    for i, s in index_to_stem.items():
        out[i] = s
    return out


def test_cluster_qualifies_isolated_does_not():
    # vuln/threat/risk cluster at 1,3,5 (span 4 <= 5+2); a lone vuln far away.
    toks = _tokens({1: "vuln", 3: "threat", 5: "risk", 50: "vuln"}, 51)
    group = (("vuln", "threat", "risk"), 5)
    assert proximity_qualifying_indices(toks, group) == {1, 3, 5}


def test_reordered_terms_still_qualify():
    # span-window ignores order: risk/threat/vuln at 0,1,2, slop 0 -> span 2 <= 2.
    toks = _tokens({0: "risk", 1: "threat", 2: "vuln"}, 3)
    group = (("vuln", "threat", "risk"), 0)
    assert proximity_qualifying_indices(toks, group) == {0, 1, 2}


def test_at_bound_qualifies():
    # a@0, b@1, c@7 -> span 7 == slop(5)+2. Exactly at the bound.
    toks = _tokens({0: "a", 1: "b", 7: "c"}, 8)
    assert proximity_qualifying_indices(toks, (("a", "b", "c"), 5)) == {0, 1, 7}


def test_one_past_bound_does_not_qualify():
    # a@0, b@1, c@8 -> span 8 > slop(5)+2 = 7.
    toks = _tokens({0: "a", 1: "b", 8: "c"}, 9)
    assert proximity_qualifying_indices(toks, (("a", "b", "c"), 5)) == set()


def test_repeated_term_only_included_when_in_window():
    # a@0, a@5, b@6, c@7, slop 0 -> bound 2. Window [5,7] covers a@5,b,c (span 2);
    # a@0 is too far (any window with a@0 + b + c spans >= 7) -> excluded.
    toks = _tokens({0: "a", 5: "a", 6: "b", 7: "c"}, 8)
    assert proximity_qualifying_indices(toks, (("a", "b", "c"), 0)) == {5, 6, 7}


def test_repeated_term_included_when_window_widens():
    # Same layout, slop 5 -> bound 7. Window [0,7] (span 7) covers all incl a@0.
    toks = _tokens({0: "a", 5: "a", 6: "b", 7: "c"}, 8)
    assert proximity_qualifying_indices(toks, (("a", "b", "c"), 5)) == {0, 5, 6, 7}


def test_missing_term_yields_nothing():
    toks = _tokens({0: "a", 1: "b"}, 5)  # no "c" anywhere
    assert proximity_qualifying_indices(toks, (("a", "b", "c"), 5)) == set()


def test_empty_group_yields_nothing():
    toks = _tokens({0: "a"}, 3)
    assert proximity_qualifying_indices(toks, ((), 5)) == set()


def test_two_clusters_both_qualify():
    toks = _tokens({0: "a", 1: "b", 2: "c", 40: "c", 41: "b", 42: "a"}, 43)
    assert proximity_qualifying_indices(toks, (("a", "b", "c"), 1)) == {0, 1, 2, 40, 41, 42}


# --- MatchSpec.proximity_groups -------------------------------------------


def test_brace_proximity_populates_group():
    spec = MatchSpec.from_query("{5}vulnerability threat risk")
    assert spec.proximity_groups == ((_stems("vulnerability", "threat", "risk"), 5),)
    # Group terms still drive ordinary word matching.
    assert _stems("vulnerability", "threat", "risk")[0] in spec.exact_stems


def test_near_proximity_populates_group():
    spec = MatchSpec.from_query("malware NEAR/3 detection")
    assert spec.proximity_groups == ((_stems("malware", "detection"), 3),)
    assert set(_stems("malware", "detection")) <= spec.exact_stems


def test_typed_phrase_slop_is_proximity_not_contiguous():
    spec = MatchSpec.from_query('"climate change"~4')
    assert spec.proximity_groups == ((_stems("climate", "change"), 4),)
    # Must NOT also be treated as a contiguous (slop-0) phrase.
    assert _stems("climate", "change") not in spec.phrases
    assert set(_stems("climate", "change")) <= spec.exact_stems


def test_plain_query_has_no_proximity_groups():
    spec = MatchSpec.from_query("vulnerability threat risk")
    assert spec.proximity_groups == ()


def test_quoted_phrase_without_slop_stays_contiguous():
    spec = MatchSpec.from_query('"climate change"')
    assert spec.proximity_groups == ()
    assert _stems("climate", "change") in spec.phrases


def test_standalone_phrase_survives_alongside_same_terms_proximity():
    # A standalone contiguous phrase and a proximity group sharing its terms must
    # coexist: the unsloped phrase stays contiguous, the sloped one is a group.
    spec = MatchSpec.from_query('"climate change" "climate change"~4')
    assert spec.proximity_groups == ((_stems("climate", "change"), 4),)
    assert _stems("climate", "change") in spec.phrases


def test_slop_zero_phrase_stays_contiguous_not_proximity():
    # "a b"~0 is slop 0 == contiguous: no proximity group, still a phrase.
    spec = MatchSpec.from_query('"climate change"~0')
    assert spec.proximity_groups == ()
    assert _stems("climate", "change") in spec.phrases


def test_proximity_only_spec_is_not_empty():
    # is_empty must agree with from_query's guard, which counts proximity_groups.
    assert not MatchSpec.from_query("{3}vulnerability threat risk").is_empty
    assert not MatchSpec(proximity_groups=((("a", "b"), 3),)).is_empty


def test_brace_alias_does_not_leak_digit_into_colour_order():
    # The {N} digit must not consume a colour slot: the first real term gets
    # slot 0 (yellow), matching a typed proximity query.
    from fnd.matching import match_color

    spec = MatchSpec.from_query("{4}vulnerability threat risk")
    assert match_color("vulnerability", spec) == 0


# --- dim style variants ----------------------------------------------------


def test_match_style_dim_differs_from_full():
    from fnd.render import match_style

    assert match_style(0, dim=True) != match_style(0)
    assert match_style(1, dim=True) != match_style(1)


def test_word_highlight_runs_dim_uses_dim_style():
    from fnd.render import match_style, word_highlight_runs

    spec = MatchSpec.from_query("vulnerability")
    full = word_highlight_runs("vulnerability", spec, dim=False)
    dim = word_highlight_runs("vulnerability", spec, dim=True)
    assert full
    assert dim
    full_styles = {style for _, _, style in full}
    dim_styles = {style for _, _, style in dim}
    assert full_styles != dim_styles
    assert match_style(0, dim=True) in dim_styles


# --- two-tier wiring in apply_match_highlights -----------------------------


def _styles_at(t: Text, offset: int) -> set[str]:
    return {str(s.style) for s in t.spans if s.start <= offset < s.end}


def test_proximity_cluster_full_lone_occurrence_dim():

    from fnd.render import DIM_MATCH_STYLES, MATCH_STYLES, apply_match_highlights

    line = "vulnerability threat risk " + ("filler " * 20) + "vulnerability"
    spec = MatchSpec.from_query("{3}vulnerability threat risk", auto_fuzzy=False)
    t = Text(line)
    assert apply_match_highlights(t, spec)

    cluster_off = line.index("vulnerability")
    lone_off = line.rindex("vulnerability")
    assert cluster_off != lone_off
    # In-cluster occurrence renders at full strength...
    assert _styles_at(t, cluster_off) & set(MATCH_STYLES)
    assert not (_styles_at(t, cluster_off) & set(DIM_MATCH_STYLES))
    # ...the far-away lone occurrence is dimmed.
    assert _styles_at(t, lone_off) & set(DIM_MATCH_STYLES)
    assert not (_styles_at(t, lone_off) & set(MATCH_STYLES))


def test_plain_query_never_dims():

    from fnd.render import DIM_MATCH_STYLES, apply_match_highlights

    line = "vulnerability here and vulnerability there far apart " + ("x " * 30) + "vulnerability"
    spec = MatchSpec.from_query("vulnerability", auto_fuzzy=False)
    t = Text(line)
    assert apply_match_highlights(t, spec)
    for off in (line.index("vulnerability"), line.rindex("vulnerability")):
        assert not (_styles_at(t, off) & set(DIM_MATCH_STYLES))


# --- LIVE preview path (the markdown widget baker, not the export path) -----


def _span_styles_at(spans: list[Span], offset: int) -> set[str]:
    return {str(s.style) for s in spans if s.start <= offset < s.end}


def test_live_markdown_baker_dims_lone_occurrence():
    # The stock FNDMarkdown widget bakes highlights via _build_match_spans —
    # this is the path the live preview actually uses, NOT apply_match_highlights.
    from fnd.render import DIM_MATCH_STYLES, MATCH_STYLES
    from fnd.tui.widgets.markdown import _build_match_spans

    plain = "vulnerability threat risk " + ("filler " * 20) + "vulnerability"
    spec = MatchSpec.from_query("{3}vulnerability threat risk", auto_fuzzy=False)
    spans = _build_match_spans(plain, spec)

    cluster_off = plain.index("vulnerability")
    lone_off = plain.rindex("vulnerability")
    assert _span_styles_at(spans, cluster_off) & set(MATCH_STYLES)
    assert _span_styles_at(spans, lone_off) & set(DIM_MATCH_STYLES)
    assert not (_span_styles_at(spans, lone_off) & set(MATCH_STYLES))

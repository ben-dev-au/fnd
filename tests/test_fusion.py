"""Parallel multi-query + RRF fusion.

Three new pieces land here:

* :func:`fnd.fusion.rrf_fuse` — pure Reciprocal Rank Fusion of ranked Hit
  lists with per-source weights and a small rank-1/2/3 position bonus.
* :func:`fnd.fusion.auto_subqueries` — derive sub-queries from a typed user
  query (phrase + lex + optional synonym).
* :func:`fnd.fusion.parse_multi_input` — parse the ``:multi`` typed
  multi-line syntax (``lex:`` / ``phrase:`` / ``syn:``).

Plus :func:`fnd.fusion.fusion_search` orchestrates the above against an
fnd :class:`fnd.query.Searcher`.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from fnd.fusion import (
    SubQuery,
    _attribute_sources,
    auto_subqueries,
    fusion_search,
    parse_multi_input,
    rrf_fuse,
)
from fnd.index import build_index
from fnd.query import Hit, Searcher
from fnd.synonyms import SynonymTable

# ── helpers ────────────────────────────────────────────────────────


def _hit(parent_id: str, *, score: float = 1.0, chunk_seq: int = 0) -> Hit:
    """Tiny Hit factory for pure-function tests; only the dedup key fields
    matter to RRF (parent_id, chunk_seq)."""
    return Hit(
        score=score,
        parent_id=parent_id,
        path=f"/{parent_id}",
        kind="md",
        page=0,
        slide=0,
        heading_path="",
        title="",
        snippet="",
        chunk_seq=chunk_seq,
    )


# ── rrf_fuse ───────────────────────────────────────────────────────


def test_rrf_fuse_empty_inputs_returns_empty() -> None:
    assert rrf_fuse([], weights=[]) == []
    assert rrf_fuse([[]], weights=[1.0]) == []


def test_rrf_fuse_single_ranking_preserves_order() -> None:
    ranking = [_hit("a"), _hit("b"), _hit("c")]
    fused = rrf_fuse([ranking], weights=[1.0])
    assert [h.parent_id for h in fused] == ["a", "b", "c"]


def test_rrf_fuse_dedupes_across_rankings() -> None:
    """A doc appearing in two rankings must appear once with summed RRF score."""
    r1 = [_hit("a"), _hit("b")]
    r2 = [_hit("b"), _hit("a")]
    fused = rrf_fuse([r1, r2], weights=[1.0, 1.0], k=60)
    ids = [h.parent_id for h in fused]
    # Each id once; relative order: a and b have symmetric contributions, so
    # tied — but one of them must come first deterministically.
    assert sorted(ids) == ["a", "b"]
    assert len(fused) == 2


def test_a_light_pass_top_does_not_jump_the_lex_runner_up() -> None:
    """Fusion is RRF alone: rank 1 of a 0.6 pass (0.6/61) stays below lex rank 2
    (1/62). The old rank-1 bonus put it above."""
    lex = [_hit("z", score=2.0), _hit("x", score=1.0)]
    syn = [_hit("y", score=9.0)]
    fused = rrf_fuse([lex, syn], weights=[1.0, 0.6])
    assert [h.parent_id for h in fused] == ["z", "x", "y"]


def test_equal_scores_in_a_pass_share_a_rank() -> None:
    """Tied sections fuse to tied scores, so grouping falls back to document order."""
    ranking = [_hit("a", score=3.0), _hit("b", score=2.0), _hit("c", score=2.0)]
    fused = rrf_fuse([ranking], weights=[1.0])
    by_id = {h.parent_id: h.rank_score for h in fused}
    assert by_id["b"] == by_id["c"] == pytest.approx(1.0 / 62.0)


def test_rrf_fuse_weight_dominance() -> None:
    """A doc only in the heavily-weighted sub-query must outrank a doc only
    in the low-weight sub-query — the weight is the multiplier on each
    sub-query's RRF contribution."""
    heavy = [_hit("h")]
    light = [_hit("l")]
    fused = rrf_fuse([heavy, light], weights=[10.0, 0.1])
    assert fused[0].parent_id == "h"


def test_a_heavy_pass_top_beats_light_pass_tops() -> None:
    """``a`` is rank 1 of the heavy exact-phrase pass and rank 2 of three
    lighter ones; ``b`` is rank 1 of the lighter ones only. ``a`` must win."""
    a, b = _hit("a", score=1.0), _hit("b", score=2.0)
    heavy = [a]  # exact phrase, weight 2.0: a at rank 1
    near = [b, a]  # weight 1.5: b rank 1, a rank 2
    loose = [b, a]  # weight 1.0
    lex = [b, a]  # weight 1.0
    fused = rrf_fuse([heavy, near, loose, lex], weights=[2.0, 1.5, 1.0, 1.0])
    assert fused[0].parent_id == "a"


def test_rrf_fuse_ranks_by_rrf_and_keeps_bm25_for_display() -> None:
    """``rank_score`` is the RRF sum; ``score`` stays the best BM25."""
    fused = rrf_fuse([[_hit("a", score=999.0)], [_hit("a", score=5.0)]], weights=[1.0, 0.5], k=60)
    assert fused[0].rank_score == pytest.approx(1.0 / 61.0 + 0.5 / 61.0, rel=1e-6)
    assert fused[0].score == 999.0


def test_rrf_fuse_dedup_uses_parent_id_and_chunk_seq() -> None:
    """Two chunks of the same file are distinct (different chunk_seq);
    same chunk twice across rankings dedupes."""
    a0 = _hit("a", chunk_seq=0)
    a1 = _hit("a", chunk_seq=1)
    fused = rrf_fuse([[a0, a1], [a1]], weights=[1.0, 1.0])
    keys = {(h.parent_id, h.chunk_seq) for h in fused}
    assert keys == {("a", 0), ("a", 1)}


# ── SubQuery / auto_subqueries ─────────────────────────────────────


def test_auto_subqueries_multiword_query_emits_phrase_and_lex() -> None:
    """Multi-word queries get a phrase pass (weight 2.0) + lex pass (1.0)."""
    subs = auto_subqueries("susy breaking", synonyms=None)
    sources = [s.source for s in subs]
    assert "phrase" in sources
    assert "lex" in sources
    phrase = next(s for s in subs if s.source == "phrase")
    lex = next(s for s in subs if s.source == "lex")
    assert phrase.query == '"susy breaking"'
    assert lex.query == "susy breaking"
    assert phrase.weight == pytest.approx(2.0)
    assert lex.weight == pytest.approx(1.0)


def test_auto_subqueries_single_word_skips_phrase() -> None:
    """A single-word query has no meaningful phrase pass — phrase("foo") ==
    term("foo") — so we only emit the lex pass."""
    subs = auto_subqueries("foo", synonyms=None)
    sources = [s.source for s in subs]
    assert sources == ["lex"]


def test_auto_subqueries_user_supplied_quotes_skip_auto_phrase() -> None:
    """When the user already wraps their query in quotes, the lex pass routes
    that through Tantivy as a PhraseQuery on its own — adding an auto-phrase
    pass would double-wrap (``""man in the middle""``) and crash the parser.
    Skip the auto-phrase pass in that case; the lex pass already carries the
    phrase semantics the user asked for.
    """
    subs = auto_subqueries('"man in the middle"', synonyms=None)
    sources = [s.source for s in subs]
    assert sources == ["lex"]
    assert subs[0].query == '"man in the middle"'


@pytest.mark.parametrize("q", ["{60} buffer overflow exploit", "alpha NEAR/5 beta"])
def test_auto_subqueries_proximity_skips_auto_phrase(q: str) -> None:
    """Proximity already encodes phrase intent in the lex pass (it expands to
    ``"a b"~N``). Auto-wrapping would yield ``""a b"~N`` and crash the parser."""
    subs = auto_subqueries(q, synonyms=None)
    assert [s.source for s in subs] == ["lex"]
    assert subs[0].query == q


def test_auto_subqueries_appends_synonym_pass_when_applicable() -> None:
    """When a query term is in a synonym group, a third sub-query (weight
    0.6) carries the expanded form."""
    table = SynonymTable.from_groups([["susy", "supersymmetry"]])
    subs = auto_subqueries("susy breaking", synonyms=table)
    sources = [s.source for s in subs]
    assert "syn" in sources
    syn = next(s for s in subs if s.source == "syn")
    assert syn.weight == pytest.approx(0.6)
    # The syn sub-query carries the expansion, not the bare query.
    assert "supersymmetry" in syn.query


def test_auto_subqueries_omits_syn_when_query_unchanged_after_expansion() -> None:
    """If synonym expansion is a no-op for this query, don't waste a Tantivy
    round-trip on a duplicate of the lex pass."""
    table = SynonymTable.from_groups([["unrelated", "other"]])
    subs = auto_subqueries("susy breaking", synonyms=table)
    assert all(s.source != "syn" for s in subs)


def test_auto_subqueries_proximity_skips_synonym_pass() -> None:
    """A ``{N}`` proximity query must NOT get a synonym pass even when a term
    has a synonym: ``expand`` would rewrite ``{20}threat intelligence`` into
    ``{20}("threat intelligence" OR ti)``, leaving an orphan ``{20}`` brace
    that Tantivy rejects. The guard mirrors the phrase pass."""
    table = SynonymTable.from_groups([["ti", "threat intelligence"]])
    subs = auto_subqueries("{20}threat intelligence", synonyms=table)
    assert all(s.source != "syn" for s in subs)


def test_auto_subqueries_near_skips_synonym_pass() -> None:
    table = SynonymTable.from_groups([["ti", "threat intelligence"]])
    subs = auto_subqueries("threat NEAR/5 intelligence", synonyms=table)
    assert all(s.source != "syn" for s in subs)


def test_auto_subqueries_field_syntax_skips_synonym_pass() -> None:
    """A field qualifier suppresses the syn pass: expanding the content portion
    in place would graft a disjunction onto the qualified query."""
    table = SynonymTable.from_groups([["ti", "threat intelligence"]])
    subs = auto_subqueries("kind:pdf threat intelligence", synonyms=table)
    assert all(s.source != "syn" for s in subs)


def test_auto_subqueries_operator_syntax_skips_synonym_pass() -> None:
    table = SynonymTable.from_groups([["mfa", "multi-factor authentication"]])
    subs = auto_subqueries("mfa AND sso", synonyms=table)
    assert all(s.source != "syn" for s in subs)


def test_auto_subqueries_quoted_query_skips_synonym_pass() -> None:
    """A user-quoted phrase carries phrase intent in the lex pass; the syn pass
    stands down so we never re-wrap it."""
    table = SynonymTable.from_groups([["ti", "threat intelligence"]])
    subs = auto_subqueries('"threat intelligence"', synonyms=table)
    assert all(s.source != "syn" for s in subs)


def test_auto_subqueries_plain_query_still_expands_synonyms() -> None:
    """The guard must not suppress the syn pass for a plain bag-of-words query —
    that is the whole point of the synonym feature."""
    table = SynonymTable.from_groups([["mfa", "multi-factor authentication"]])
    subs = auto_subqueries("reset mfa", synonyms=table)
    syn = next((s for s in subs if s.source == "syn"), None)
    assert syn is not None
    assert "multi-factor authentication" in syn.query


def test_auto_subqueries_empty_query_returns_empty() -> None:
    assert auto_subqueries("", synonyms=None) == []
    assert auto_subqueries("   ", synonyms=None) == []


# ── parse_multi_input ──────────────────────────────────────────────


def test_parse_multi_input_basic() -> None:
    text = 'lex: susy breaking\nphrase: "soft breaking"\nsyn: susy'
    result = parse_multi_input(text, synonyms=None)
    sources = [s.source for s in result.subqueries]
    assert sources == ["lex", "phrase", "syn"]
    assert result.intent is None


def test_parse_multi_input_uses_default_weights_per_source() -> None:
    """`lex:` defaults to weight 1.0, `phrase:` to 2.0, `syn:` to 0.6 —
    matching the auto-mode weights."""
    result = parse_multi_input('lex: a\nphrase: "b c"\nsyn: a', synonyms=None)
    by_source = {s.source: s for s in result.subqueries}
    assert by_source["lex"].weight == pytest.approx(1.0)
    assert by_source["phrase"].weight == pytest.approx(2.0)
    assert by_source["syn"].weight == pytest.approx(0.6)


def test_parse_multi_input_phrase_quotes_value_if_unquoted() -> None:
    """`phrase: foo bar` (no quotes) becomes the Tantivy phrase `"foo bar"`."""
    result = parse_multi_input("phrase: foo bar", synonyms=None)
    assert result.subqueries[0].query == '"foo bar"'


def test_parse_multi_input_syn_uses_synonym_table_to_expand() -> None:
    """A `syn:` line must expand against the supplied table."""
    table = SynonymTable.from_groups([["susy", "supersymmetry"]])
    result = parse_multi_input("syn: susy", synonyms=table)
    assert "supersymmetry" in result.subqueries[0].query


def test_parse_multi_input_skips_blank_lines_and_unknown_prefixes() -> None:
    """Lines without a recognised prefix (or blanks/comments) are ignored."""
    text = "\n# a comment\nlex: a\n\nbogus: b\nlex: c\n"
    result = parse_multi_input(text, synonyms=None)
    queries = [s.query for s in result.subqueries]
    assert queries == ["a", "c"]


# ── fusion_search end-to-end ───────────────────────────────────────


@pytest.fixture
def fusion_corpus(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """Three docs that exercise phrase / lex / syn distinction.

    * adjacent.md — both query terms next to each other (phrase pass wins)
    * scattered.md — both terms in the doc but not adjacent (lex matches,
      phrase does not)
    * synonym.md — only the long-form synonym appears (syn pass surfaces)
    """
    root = tmp_path / "docs"
    root.mkdir(parents=True)
    (root / "adjacent.md").write_text(
        "# A\nThe susy breaking sector dominates here.\n",
        encoding="utf-8",
    )
    (root / "scattered.md").write_text(
        "# B\nThe susy hierarchy is interesting and a breaking effect arises later.\n",
        encoding="utf-8",
    )
    (root / "synonym.md").write_text(
        "# C\nSupersymmetry breaking constraints follow.\n",
        encoding="utf-8",
    )
    build_index(roots=[tmp_path], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


def test_fusion_search_returns_phrase_match_first(fusion_corpus: Path) -> None:
    """The doc with adjacent terms must rank above the scattered one — the
    phrase sub-query (weight 2.0) lifts it past the lex-only contribution."""
    s = Searcher(index_dir=fusion_corpus)
    hits = fusion_search(s, query="susy breaking", limit=10)
    paths = [Path(h.path).name for h in hits]
    assert "adjacent.md" in paths
    assert "scattered.md" in paths
    assert paths.index("adjacent.md") < paths.index("scattered.md")


def test_fusion_search_synonym_pass_finds_long_form(
    fusion_corpus: Path,
) -> None:
    """With a synonym table mapping susy↔supersymmetry, the synonym.md doc
    surfaces even though it never uses the literal term ``susy``."""
    s = Searcher(index_dir=fusion_corpus)
    table = SynonymTable.from_groups([["susy", "supersymmetry"]])
    hits = fusion_search(s, query="susy breaking", limit=10, synonyms=table)
    paths = [Path(h.path).name for h in hits]
    assert "synonym.md" in paths


def test_fusion_search_dedup_across_subqueries(fusion_corpus: Path) -> None:
    """A doc found by multiple sub-queries appears once in the fused list."""
    s = Searcher(index_dir=fusion_corpus)
    hits = fusion_search(s, query="susy breaking", limit=10)
    keys = [(h.parent_id, h.chunk_seq) for h in hits]
    assert len(keys) == len(set(keys))


def test_fusion_search_zero_matches_returns_empty(fusion_corpus: Path) -> None:
    s = Searcher(index_dir=fusion_corpus)
    assert fusion_search(s, query="zzznothingmatcheszzz", limit=10) == []


def test_fusion_search_subqueries_override_auto_derivation(
    fusion_corpus: Path,
) -> None:
    """Passing explicit sub-queries (e.g. from `:multi`) bypasses
    auto_subqueries — only the supplied list runs.

    With a single ``lex`` sub-query, no hit can be primarily attributed to
    the synonym or phrase passes (those weren't issued).
    """
    s = Searcher(index_dir=fusion_corpus)
    subs = [SubQuery(query="susy breaking", weight=1.0, source="lex")]
    hits = fusion_search(s, query="susy breaking", limit=10, subqueries=subs)
    assert hits, "lex pass should still surface adjacent.md / scattered.md"
    # Every hit must come from the lex sub-query → pass_index 0.
    assert all(h.pass_index == 0 for h in hits)


def test_fusion_search_pass_index_3_for_phrase_primary(
    fusion_corpus: Path,
) -> None:
    """Hits primarily surfaced by the phrase pass are tagged pass_index=3
    (new fusion source) so the TUI shows a phrase glyph."""
    s = Searcher(index_dir=fusion_corpus)
    hits = fusion_search(s, query="susy breaking", limit=10)
    adjacent = next(h for h in hits if Path(h.path).name == "adjacent.md")
    # The adjacent doc should be primarily attributed to the phrase sub-query.
    assert adjacent.pass_index == 3


def test_fusion_search_pass_index_2_for_synonym_primary(
    fusion_corpus: Path,
) -> None:
    """A doc the synonym pass ranks above the literal pass is tagged pass_index=2,
    matching the cascade synonym-pass glyph (⊕), though it also matches "breaking"."""
    s = Searcher(index_dir=fusion_corpus)
    table = SynonymTable.from_groups([["susy", "supersymmetry"]])
    hits = fusion_search(s, query="susy breaking", limit=10, synonyms=table)
    syn_hit = next(h for h in hits if Path(h.path).name == "synonym.md")
    assert syn_hit.pass_index == 2


def test_fusion_search_degrades_malformed_subquery(fusion_corpus: Path) -> None:
    """A malformed sub-query must degrade to an empty ranking, never abort the
    whole fused search — the other sub-queries still return their hits."""
    s = Searcher(index_dir=fusion_corpus)
    subs = [
        SubQuery(query="susy breaking", weight=1.0, source="lex"),
        SubQuery(query="page:[10 TO]", weight=0.6, source="syn"),  # a range Tantivy rejects
    ]
    hits = fusion_search(s, query="susy breaking", limit=10, subqueries=subs)
    assert hits, "the valid lex sub-query must still surface results"


def test_fusion_trace_records_degraded_subquery(fusion_corpus: Path) -> None:
    """The degraded sub-query is recorded on the trace (degraded flag, zero
    hits) so ``:explain`` shows it stood down rather than silently vanishing."""
    s = Searcher(index_dir=fusion_corpus)
    subs = [
        SubQuery(query="susy breaking", weight=1.0, source="lex"),
        SubQuery(query="page:[10 TO]", weight=0.6, source="syn"),
    ]
    _hits, trace = fusion_search(
        s, query="susy breaking", limit=10, subqueries=subs, with_trace=True
    )
    syn_trace = next(t for t in trace.subqueries if t.source == "syn")
    assert syn_trace.degraded is True
    assert syn_trace.hit_count == 0
    lex_trace = next(t for t in trace.subqueries if t.source == "lex")
    assert lex_trace.degraded is False


# ── TUI glyph for phrase ───────────────────────────────────────────


def test_format_hit_label_shows_phrase_glyph_for_pass_index_3() -> None:
    """The fusion phrase pass gets its own glyph (e.g. ❝) so the TUI tree
    visually distinguishes phrase-led hits from lex/fuzzy/synonym ones."""
    from fnd.tui.results_labels import _format_hit_label

    base = Hit(
        score=1.23,
        parent_id="x",
        path="/x",
        kind="md",
        page=7,
        slide=0,
        heading_path="",
        title="",
        snippet="",
    )
    phrase_label = _format_hit_label(replace(base, pass_index=3))
    lex_label = _format_hit_label(base)
    # Phrase label has a glyph (any non-existing-pass-glyph character will do
    # so long as it's distinct from the lex/fuzzy/synonym glyphs).
    assert phrase_label != lex_label
    assert "~" not in phrase_label  # not the fuzzy glyph
    assert "⊕" not in phrase_label  # not the synonym glyph


def test_the_phrase_glyph_wins_wherever_the_phrase_pass_found_the_doc() -> None:
    """``a`` is lex rank 1 and phrase rank 2; it still contains the exact phrase."""
    a, b = _hit("a", score=2.0), _hit("b", score=1.0)
    subs = [SubQuery("p", 2.0, "phrase"), SubQuery("l", 1.0, "lex")]
    sources = _attribute_sources([[b, a], [a, b]], subs)
    assert sources[("a", 0)] == "phrase"


def test_lex_and_synonym_split_by_rank_with_lex_on_a_tie() -> None:
    led, both = _hit("led", score=3.0), _hit("both", score=2.0)
    lex = [_hit("x", score=9.0), _hit("y", score=8.0), both, led]
    syn = [led, both]
    subs = [SubQuery("l", 1.0, "lex"), SubQuery("s", 0.6, "syn")]
    sources = _attribute_sources([lex, syn], subs)
    assert sources[("led", 0)] == "syn"
    assert sources[("both", 0)] == "syn"
    tie = _attribute_sources([[_hit("t")], [_hit("t")]], subs)
    assert tie[("t", 0)] == "lex"

"""Parallel multi-query + Reciprocal Rank Fusion (§9d).

Phase 8's cascade widens *sequentially* — only run the next pass if the
previous one came up short. Phase 9 runs sub-queries *in parallel* and
fuses them with Reciprocal Rank Fusion (RRF), so a doc that ranks well in
several sub-queries gets a real boost. The two mechanisms are
complementary; the search layer can use either.

Auto-derived sub-queries (per the worked example in §9d):

* ``phrase`` — the user's query wrapped in quotes (weight 2.0). Only emitted
  for multi-word queries (a phrase pass on a single word is identical to
  the lex pass and would just inflate the RRF score for everything).
* ``lex`` — the literal user query (weight 1.0). Implicit AND across terms.
* ``syn`` — synonym-expanded version of the query (weight 0.6). Only emitted
  when the expansion actually changes the query string.

The ``stem`` sub-query mentioned in §9d is omitted because the body field
is already analyzed with ``en_stem`` (Snowball English) — an explicit
stemmed pass would duplicate the lex pass.

Pass-index attribution: each fused hit is tagged with ``pass_index``
matching the highest-weighted sub-query that surfaced it. This lets the
TUI render a per-source glyph using the same vocabulary as cascade
(``~`` fuzzy, ``⊕`` synonym) plus a new glyph for fusion-phrase hits
(``pass_index=3``).

Strong-signal bypass (UX-pass-4 §1) and the ``intent:`` line in
:func:`parse_multi_input` (§3) are adapted from `tobi/qmd
<https://github.com/tobi/qmd>`_ (MIT). The score normalization
``s / (1 + s)`` and the threshold values 0.85 / 0.15 come from QMD;
the implementation here is a Python rewrite. See README acknowledgments.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, overload

from acorn.explain import FusionTrace, HitContribution, SubQueryTrace
from acorn.query import Hit, Searcher
from acorn.synonyms import SynonymTable, expand

# RRF constant; default 60 matches the original Cormack/Clarke/Buettcher 2009
# paper and what QMD uses.
_RRF_K_DEFAULT = 60

# Tiny additive bonus for being rank 1 / 2 / 3 in any sub-query. Tunes RRF
# (which is otherwise smooth) toward a slight preference for the "very top"
# of any single sub-query — matches the §9d spec.
_POS_BONUS_RANK_1 = 0.05
_POS_BONUS_RANK_2_3 = 0.02

# Default per-source weights (§9d worked example).
_DEFAULT_WEIGHTS: dict[str, float] = {
    "phrase": 2.0,
    "lex": 1.0,
    "syn": 0.6,
}

# Map source name → pass_index used by the TUI glyph table.
# Keep aligned with cascade: 0 = neutral (lex/exact), 1 = fuzzy,
# 2 = synonym, 3 = fusion-phrase (new in phase 9).
_SOURCE_TO_PASS_INDEX: dict[str, int] = {
    "lex": 0,
    "fuzzy": 1,
    "syn": 2,
    "phrase": 3,
}

# Strong-signal bypass thresholds (UX-pass-4 §1). Operate on a normalized
# BM25 score ``s_norm = s / (1 + s)``, monotone in [0, 1) — query-
# independent and corpus-stable. Adapted from tobi/qmd (MIT) — see
# README acknowledgments.
STRONG_SIGNAL_MIN_NORM_SCORE: float = 0.85
STRONG_SIGNAL_MIN_NORM_GAP: float = 0.15


def normalize_bm25(score: float) -> float:
    """Map a raw BM25 score (positive, unbounded) into ``[0, 1)``.

    The transform ``s / (1 + s)`` is asymptotic to 1: it preserves
    ordering, compresses gaps at high values (so 30 vs 31 is barely
    distinguishable from 31 vs 32), and amplifies gaps at low values
    (so 1.5 vs 0.5 is meaningful). Matches the threshold semantics
    needed by strong-signal bypass.
    """
    if score <= 0.0:
        return 0.0
    return score / (1.0 + score)


@dataclass(slots=True, frozen=True)
class SubQuery:
    """One parallel sub-query.

    ``query`` is a Tantivy-parseable string. ``weight`` multiplies its RRF
    contribution. ``source`` is a short tag (``lex`` / ``phrase`` / ``syn``
    / ``fuzzy``) used both for weighting defaults and for per-pass glyph
    attribution.
    """

    query: str
    weight: float
    source: str


@dataclass(slots=True, frozen=True)
class MultiInput:
    """Result of parsing a ``:multi`` block (UX-pass-4 §3).

    ``intent`` does NOT produce a sub-query. It influences:

    * regime triage — intent disables strong-signal bypass
      (:func:`acorn.layered._evaluate_strong_signal`)
    * snippet selection — chunks containing intent tokens preferred
      (:func:`acorn.query._make_snippet`)
    * forward-compat with the §22 LLM cross-encoder reranker (deferred
      to v2).
    """

    subqueries: list[SubQuery]
    intent: str | None = None


def rrf_fuse(
    rankings: list[list[Hit]],
    *,
    weights: list[float],
    k: int = _RRF_K_DEFAULT,
) -> list[Hit]:
    """Fuse parallel ranked lists with Reciprocal Rank Fusion.

    Formula (per ranking, per doc d at rank r, 1-indexed)::

        contribution = weight / (k + r) + position_bonus(r)

    where ``position_bonus`` is +0.05 at rank 1, +0.02 at ranks 2-3,
    0 otherwise. Final score is the sum across all rankings; output is
    deduplicated by ``(parent_id, chunk_seq)`` and sorted descending.

    The returned :class:`Hit` records carry the *fused* score in ``score``
    (the original BM25 is discarded after fusion — re-sorting downstream
    must use this fused score).
    """
    if not rankings or not any(rankings):
        return []
    assert len(rankings) == len(weights), "weights must match rankings count"

    fused_score: dict[tuple[str, int], float] = {}
    representative: dict[tuple[str, int], Hit] = {}
    # Track the source of the largest single contribution per doc; this drives
    # ``pass_index`` attribution after fusion.
    top_source_weight: dict[tuple[str, int], float] = {}

    for ranking, weight in zip(rankings, weights, strict=True):
        for rank, hit in enumerate(ranking, start=1):
            key = (hit.parent_id, hit.chunk_seq)
            contribution = weight / (k + rank)
            if rank == 1:
                contribution += _POS_BONUS_RANK_1
            elif rank in (2, 3):
                contribution += _POS_BONUS_RANK_2_3
            fused_score[key] = fused_score.get(key, 0.0) + contribution
            # Keep the first-seen Hit object as the representative — its body
            # snippet, page, etc. are equivalent across rankings (same chunk).
            representative.setdefault(key, hit)
            if contribution > top_source_weight.get(key, -1.0):
                top_source_weight[key] = contribution

    out: list[Hit] = []
    for key, total in fused_score.items():
        rep = representative[key]
        out.append(_with_score(rep, total))
    out.sort(key=lambda h: h.score, reverse=True)
    return out


def auto_subqueries(query: str, *, synonyms: SynonymTable | None) -> list[SubQuery]:
    """Derive parallel sub-queries from a typed user query.

    Multi-word queries get a ``phrase`` + ``lex`` pair; single-word queries
    only get ``lex`` (a phrase of one word is identical to a term query
    and would just dilute the RRF math). A ``syn`` pass is appended only
    when synonym expansion actually rewrites the query — otherwise it
    would issue an identical Tantivy round-trip for nothing.
    """
    q = query.strip()
    if not q:
        return []
    subs: list[SubQuery] = []
    # When the user already supplied quotes they've encoded the phrase intent
    # into the lex pass — Tantivy parses ``"a b c"`` as a PhraseQuery directly.
    # Re-wrapping into ``""a b c""`` would double-quote and crash the parser.
    user_wrote_phrase = '"' in q
    if len(q.split()) >= 2 and not user_wrote_phrase:
        subs.append(SubQuery(query=f'"{q}"', weight=_DEFAULT_WEIGHTS["phrase"], source="phrase"))
    subs.append(SubQuery(query=q, weight=_DEFAULT_WEIGHTS["lex"], source="lex"))
    if synonyms is not None and synonyms.groups:
        expanded = expand(q, synonyms)
        if expanded != q:
            subs.append(SubQuery(query=expanded, weight=_DEFAULT_WEIGHTS["syn"], source="syn"))
    return subs


def parse_multi_input(text: str, *, synonyms: SynonymTable | None) -> MultiInput:
    """Parse the ``:multi`` typed-input syntax into a :class:`MultiInput`.

    Each non-blank line is ``<source>: <value>``. Recognised sources are
    ``lex``, ``phrase``, ``syn``, and ``intent``. Lines starting with
    ``#`` are comments. Unknown prefixes are ignored (the TUI surfaces a
    parse error inline; this function stays permissive so a typo in one
    line doesn't abort a usable multi-line query).

    A ``phrase:`` value that isn't already quoted is wrapped in quotes so
    the Tantivy parser sees it as a phrase query.
    A ``syn:`` value is expanded against the supplied table at parse time.
    An ``intent:`` line is captured separately — it does NOT produce a
    sub-query, but is returned on the :class:`MultiInput` so callers can
    pass it to :func:`acorn.layered.search_layered`. Last-write-wins if
    multiple intent lines appear (matches QMD's "at most one intent
    line" rule).
    """
    subs: list[SubQuery] = []
    intent: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        prefix, value = line.split(":", 1)
        prefix = prefix.strip().lower()
        value = value.strip()
        if not value:
            continue
        if prefix == "intent":
            intent = value
            continue
        if prefix not in _DEFAULT_WEIGHTS:
            continue
        if prefix == "phrase" and not (value.startswith('"') and value.endswith('"')):
            value = f'"{value}"'
        elif prefix == "syn" and synonyms is not None and synonyms.groups:
            value = expand(value, synonyms)
        subs.append(SubQuery(query=value, weight=_DEFAULT_WEIGHTS[prefix], source=prefix))
    return MultiInput(subqueries=subs, intent=intent)


@overload
def fusion_search(
    searcher: Searcher,
    *,
    query: str,
    limit: int = ...,
    collection: str | None = ...,
    synonyms: SynonymTable | None = ...,
    subqueries: list[SubQuery] | None = ...,
    metadata_filter: str | None = ...,
    active_sources: list[str] | None = ...,
    precomputed_lex_ranking: list[Hit] | None = ...,
    intent: str | None = ...,
    with_trace: Literal[False] = False,
) -> list[Hit]: ...


@overload
def fusion_search(
    searcher: Searcher,
    *,
    query: str,
    limit: int = ...,
    collection: str | None = ...,
    synonyms: SynonymTable | None = ...,
    subqueries: list[SubQuery] | None = ...,
    metadata_filter: str | None = ...,
    active_sources: list[str] | None = ...,
    precomputed_lex_ranking: list[Hit] | None = ...,
    intent: str | None = ...,
    with_trace: Literal[True],
) -> tuple[list[Hit], FusionTrace]: ...


def fusion_search(
    searcher: Searcher,
    *,
    query: str,
    limit: int = 50,
    collection: str | None = None,
    synonyms: SynonymTable | None = None,
    subqueries: list[SubQuery] | None = None,
    metadata_filter: str | None = None,
    active_sources: list[str] | None = None,
    precomputed_lex_ranking: list[Hit] | None = None,
    intent: str | None = None,
    with_trace: bool = False,
) -> list[Hit] | tuple[list[Hit], FusionTrace]:
    """Run sub-queries in parallel and RRF-fuse the results.

    When ``subqueries`` is None, calls :func:`auto_subqueries`. When
    explicit sub-queries are supplied (e.g. from a ``:multi`` panel),
    auto-derivation is skipped — only the supplied list runs.

    Each sub-query is issued through ``searcher._filtered_raw_hits`` so
    the metadata filter (frontmatter post-filter) and the ``source_path``
    scope apply to every sub-ranking. Sub-queries see identical
    analyzer/field-boost configuration as the single-pass search path.
    Results are deduplicated by ``(parent_id, chunk_seq)`` and sorted
    by RRF position; ``pass_index`` is set from the highest-weighted
    contributing source.

    **Score semantics**: RRF is used for *ordering*. The returned
    ``Hit.score`` is the maximum BM25 score across the sub-queries that
    surfaced the doc — so the TUI's score column stays in the BM25
    range users can compare to (typically 1-40 for templates-style
    queries) rather than the 0.0001-0.07 range RRF arithmetic would
    produce. The internal RRF total still drives the sort order.

    ``precomputed_lex_ranking`` (UX-pass-4 §1): when supplied, the lex
    sub-query reuses this list instead of issuing a fresh
    ``_filtered_raw_hits`` call. Lets the regime probe in
    :mod:`acorn.layered` double as fusion's lex pass — saves one Tantivy
    round-trip on every non-bypass query.

    ``with_trace`` (UX-pass-4 §2): when ``True``, returns
    ``(hits, FusionTrace)`` so callers (CLI ``--explain`` / TUI
    ``:explain``) can inspect which sub-queries ran, per-hit BM25
    scores, RRF contributions, and primary-source attribution. Default
    ``False`` returns the existing ``list[Hit]`` unchanged.
    """
    subs = subqueries if subqueries is not None else auto_subqueries(query, synonyms=synonyms)
    if not subs:
        if with_trace:
            return [], _empty_fusion_trace(query)
        return []

    rankings: list[list[Hit]] = []
    for sub in subs:
        if sub.source == "lex" and precomputed_lex_ranking is not None:
            # Reuse the regime probe's literal-pass result so we don't
            # re-issue the same Tantivy query.
            rankings.append(precomputed_lex_ranking)
        else:
            rankings.append(
                searcher._filtered_raw_hits(
                    sub.query,
                    # Oversample per sub-query so the post-fusion grouper has
                    # enough chunks to fill ``limit`` files. Mirrors the
                    # ``target=limit * 10`` contract the old single-pass
                    # ``Searcher.search_grouped`` used.
                    target=limit * 10,
                    collection=collection,
                    metadata_filter=metadata_filter,
                    active_sources=active_sources,
                    intent=intent,
                )
            )

    weights = [s.weight for s in subs]
    fused = rrf_fuse(rankings, weights=weights)

    primary_source = _attribute_sources(rankings, subs)
    bm25_scores = _bm25_score_map(rankings)
    out: list[Hit] = []
    for h in fused[:limit]:
        key = (h.parent_id, h.chunk_seq)
        src = primary_source.get(key, "lex")
        # Restore the BM25 score from whichever sub-query surfaced this
        # doc. RRF replaced ``score`` with the fused position-based
        # value; for the user-facing score we want the original BM25.
        bm25 = bm25_scores.get(key, h.score)
        out.append(_with_pass_index(_with_score(h, bm25), _SOURCE_TO_PASS_INDEX.get(src, 0)))

    if not with_trace:
        return out
    trace = _build_fusion_trace(query, subs, rankings, primary_source, out)
    return out, trace


def _build_fusion_trace(
    query: str,
    subs: list[SubQuery],
    rankings: list[list[Hit]],
    primary_source: dict[tuple[str, int], str],
    out: list[Hit],
) -> FusionTrace:
    sub_traces = [
        SubQueryTrace(
            source=s.source,
            query=s.query,
            weight=s.weight,
            hit_count=len(r),
            bm25_top=r[0].score if r else 0.0,
            bm25_second=r[1].score if len(r) > 1 else 0.0,
            rrf_k=_RRF_K_DEFAULT,
        )
        for s, r in zip(subs, rankings, strict=True)
    ]
    # Per-hit contributions: walk each ranking once, accumulate
    # rank/bm25/rrf for each (parent_id, chunk_seq) appearing in ``out``.
    out_keys = {(h.parent_id, h.chunk_seq) for h in out}
    bm25_per: dict[tuple[str, int], dict[str, float]] = {k: {} for k in out_keys}
    rank_per: dict[tuple[str, int], dict[str, int]] = {k: {} for k in out_keys}
    rrf_per: dict[tuple[str, int], dict[str, float]] = {k: {} for k in out_keys}
    for ranking, sub in zip(rankings, subs, strict=True):
        for rank, h in enumerate(ranking, start=1):
            key = (h.parent_id, h.chunk_seq)
            if key not in bm25_per:
                continue
            rank_per[key][sub.source] = rank
            bm25_per[key][sub.source] = h.score
            rrf = sub.weight / (_RRF_K_DEFAULT + rank)
            if rank == 1:
                rrf += _POS_BONUS_RANK_1
            elif rank in (2, 3):
                rrf += _POS_BONUS_RANK_2_3
            rrf_per[key][sub.source] = rrf

    contribution_traces = [
        HitContribution(
            parent_id=h.parent_id,
            chunk_seq=h.chunk_seq,
            bm25_per_source=bm25_per[(h.parent_id, h.chunk_seq)],
            rank_per_source={
                s.source: rank_per[(h.parent_id, h.chunk_seq)].get(s.source, 0) for s in subs
            },
            rrf_per_source=rrf_per[(h.parent_id, h.chunk_seq)],
            fused_total=sum(rrf_per[(h.parent_id, h.chunk_seq)].values()),
            primary_source=primary_source.get((h.parent_id, h.chunk_seq), "lex"),
            final_score=h.score,
        )
        for h in out
    ]
    return FusionTrace(
        query=query,
        subqueries=sub_traces,
        contributions=contribution_traces,
        rrf_k=_RRF_K_DEFAULT,
        pos_bonus_rank_1=_POS_BONUS_RANK_1,
        pos_bonus_rank_2_3=_POS_BONUS_RANK_2_3,
        default_weights=dict(_DEFAULT_WEIGHTS),
    )


def _empty_fusion_trace(query: str) -> FusionTrace:
    return FusionTrace(
        query=query,
        subqueries=[],
        contributions=[],
        rrf_k=_RRF_K_DEFAULT,
        pos_bonus_rank_1=_POS_BONUS_RANK_1,
        pos_bonus_rank_2_3=_POS_BONUS_RANK_2_3,
        default_weights=dict(_DEFAULT_WEIGHTS),
    )


def _bm25_score_map(rankings: list[list[Hit]]) -> dict[tuple[str, int], float]:
    """Per-doc max BM25 score across sub-queries — the score the user
    sees in the result row, regardless of which sub-query won fusion."""
    out: dict[tuple[str, int], float] = {}
    for ranking in rankings:
        for hit in ranking:
            key = (hit.parent_id, hit.chunk_seq)
            if hit.score > out.get(key, float("-inf")):
                out[key] = hit.score
    return out


def _attribute_sources(
    rankings: list[list[Hit]], subs: list[SubQuery]
) -> dict[tuple[str, int], str]:
    """Pick a primary source for each doc — the sub-query whose RRF
    contribution to that doc was largest. Ties favour the higher-weighted
    sub-query (so ``phrase`` beats ``lex`` for adjacent-term docs)."""
    primary_source: dict[tuple[str, int], str] = {}
    primary_value: dict[tuple[str, int], float] = {}
    for ranking, sub in zip(rankings, subs, strict=True):
        for rank, hit in enumerate(ranking, start=1):
            contribution = sub.weight / (_RRF_K_DEFAULT + rank)
            if rank == 1:
                contribution += _POS_BONUS_RANK_1
            elif rank in (2, 3):
                contribution += _POS_BONUS_RANK_2_3
            key = (hit.parent_id, hit.chunk_seq)
            if contribution > primary_value.get(key, -1.0):
                primary_value[key] = contribution
                primary_source[key] = sub.source
    return primary_source


def _with_score(h: Hit, score: float) -> Hit:
    return Hit(
        score=score,
        parent_id=h.parent_id,
        path=h.path,
        kind=h.kind,
        page=h.page,
        slide=h.slide,
        heading_path=h.heading_path,
        title=h.title,
        snippet=h.snippet,
        page_label=h.page_label,
        chunk_seq=h.chunk_seq,
        mtime=h.mtime,
        pass_index=h.pass_index,
        meta_blob=h.meta_blob,
    )


def _with_pass_index(h: Hit, pass_index: int) -> Hit:
    return Hit(
        score=h.score,
        parent_id=h.parent_id,
        path=h.path,
        kind=h.kind,
        page=h.page,
        slide=h.slide,
        heading_path=h.heading_path,
        title=h.title,
        snippet=h.snippet,
        page_label=h.page_label,
        chunk_seq=h.chunk_seq,
        mtime=h.mtime,
        pass_index=pass_index,
        meta_blob=h.meta_blob,
    )

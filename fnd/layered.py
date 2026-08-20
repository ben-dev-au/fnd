"""Regime-aware layered search (§9a + §9c + §9d + UX-pass-4 §1).

One entry point — :func:`search_layered` — chosen by both the TUI and
the CLI. Encapsulates the three search regimes through a single
decision tree:

* **strong-signal** (UX-pass-4 §1): literal probe alone, when the
  normalized top BM25 ≥ 0.85 AND gap ≥ 0.15 AND no intent provided.
  Bypasses fusion's phrase + syn passes entirely.
* **fusion** (§9d): phrase + lex + syn sub-queries, RRF-fused. Default.
* **cascade** (§9c): widening fallback when fusion's chunk pool is
  sparse (< limit / 4). Adds fuzzy~1 and synonym passes.

The regime decision logic lives here, not scattered across modules.
The :class:`SearchTrace` returned when ``with_trace=True`` records
which regime fired and why.

Strong-signal bypass adapted from tobi/qmd (MIT) — see README.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, overload

from fnd.cascade import cascade_search
from fnd.explain import CascadeTrace, SearchTrace, StrongSignalTrace
from fnd.fusion import (
    STRONG_SIGNAL_MIN_NORM_GAP,
    STRONG_SIGNAL_MIN_NORM_SCORE,
    fusion_search,
    normalise_bm25,
)
from fnd.query import FileGroup, Hit, Searcher, group_by_file

if TYPE_CHECKING:
    from fnd.tag_query import TagFilter
from fnd.synonyms import SynonymTable


@overload
def search_layered(
    searcher: Searcher,
    *,
    query: str,
    limit: int,
    sections_per_file: int = ...,
    sections_score_threshold: float = ...,
    collection: str | list[str] | None = ...,
    synonyms: SynonymTable | None = ...,
    metadata_filter: str | None = ...,
    active_sources: list[str] | None = ...,
    intent: str | None = ...,
    profile: object | None = ...,
    auto_fuzzy_enabled: bool = ...,
    min_term_chars: int = ...,
    tag_filter: TagFilter | None = ...,
    with_trace: Literal[False] = False,
) -> list[FileGroup]: ...


@overload
def search_layered(
    searcher: Searcher,
    *,
    query: str,
    limit: int,
    sections_per_file: int = ...,
    sections_score_threshold: float = ...,
    collection: str | list[str] | None = ...,
    synonyms: SynonymTable | None = ...,
    metadata_filter: str | None = ...,
    active_sources: list[str] | None = ...,
    intent: str | None = ...,
    profile: object | None = ...,
    auto_fuzzy_enabled: bool = ...,
    min_term_chars: int = ...,
    tag_filter: TagFilter | None = ...,
    with_trace: Literal[True],
) -> tuple[list[FileGroup], SearchTrace]: ...


def search_layered(
    searcher: Searcher,
    *,
    query: str,
    limit: int,
    sections_per_file: int = 10,
    sections_score_threshold: float = 0.0,
    collection: str | list[str] | None = None,
    synonyms: SynonymTable | None = None,
    metadata_filter: str | None = None,
    active_sources: list[str] | None = None,
    intent: str | None = None,
    profile: object | None = None,
    auto_fuzzy_enabled: bool = True,
    min_term_chars: int = 0,
    tag_filter: TagFilter | None = None,
    with_trace: bool = False,
) -> list[FileGroup] | tuple[list[FileGroup], SearchTrace]:
    """Run the regime-aware search and return ranked :class:`FileGroup`s.

    See module docstring for regime semantics. The probe doubles as
    fusion's lex sub-query when bypass does NOT fire — saving one
    Tantivy round-trip per non-bypass query.
    """
    if not query.strip():
        return ([], _empty_trace(query, intent)) if with_trace else []

    chunk_pool = limit * 10

    # Step 1: literal probe. Doubles as the bypass-decision input AND
    # (when bypass fires) the result set. When bypass does NOT fire,
    # fusion reuses this as its precomputed lex ranking — no wasted
    # Tantivy round-trip.
    probe = searcher._filtered_raw_hits(
        query,
        target=chunk_pool,
        collection=collection,
        metadata_filter=metadata_filter,
        active_sources=active_sources,
        intent=intent,
        tag_filter=tag_filter,
    )

    # Step 2: strong-signal check. Disabled when intent is supplied —
    # the obvious BM25 match may not be what the caller wants.
    ss_trace = _evaluate_strong_signal(probe, intent_present=bool(intent))
    fusion_trace = None
    cascade_trace: CascadeTrace | None = None

    if ss_trace.fired:
        hits = probe
        regime = "strong-signal"
    else:
        # Step 3: fusion (default).
        if with_trace:
            hits, fusion_trace = fusion_search(
                searcher,
                query=query,
                limit=chunk_pool,
                collection=collection,
                synonyms=synonyms,
                metadata_filter=metadata_filter,
                active_sources=active_sources,
                precomputed_lex_ranking=probe,
                intent=intent,
                tag_filter=tag_filter,
                with_trace=True,
            )
        else:
            hits = fusion_search(
                searcher,
                query=query,
                limit=chunk_pool,
                collection=collection,
                synonyms=synonyms,
                metadata_filter=metadata_filter,
                active_sources=active_sources,
                precomputed_lex_ranking=probe,
                intent=intent,
                tag_filter=tag_filter,
            )

        regime = "fusion"
        # Step 4: cascade fallback when fusion's chunk pool is sparse.
        if len(hits) < max(1, limit // 4):
            if with_trace:
                cascade_hits, cascade_trace = cascade_search(
                    searcher,
                    query=query,
                    threshold=chunk_pool,
                    limit=chunk_pool,
                    collection=collection,
                    synonyms=synonyms,
                    metadata_filter=metadata_filter,
                    active_sources=active_sources,
                    tag_filter=tag_filter,
                    intent=intent,
                    auto_fuzzy_enabled=auto_fuzzy_enabled,
                    min_term_chars=min_term_chars,
                    with_trace=True,
                )
            else:
                cascade_hits = cascade_search(
                    searcher,
                    query=query,
                    threshold=chunk_pool,
                    limit=chunk_pool,
                    collection=collection,
                    synonyms=synonyms,
                    metadata_filter=metadata_filter,
                    active_sources=active_sources,
                    tag_filter=tag_filter,
                    intent=intent,
                    auto_fuzzy_enabled=auto_fuzzy_enabled,
                    min_term_chars=min_term_chars,
                )
            if len(cascade_hits) > len(hits):
                hits = cascade_hits
                regime = _cascade_regime_label(cascade_trace) if cascade_trace else "cascade"

    # Step 5: rerank + group. Identical for every regime so all three
    # search paths produce identically-shaped FileGroups.
    if profile is not None:
        from fnd.rerank import RankingProfile, rerank_hits

        assert isinstance(profile, RankingProfile)
        hits = rerank_hits(hits, profile=profile, query=query)

    groups = group_by_file(
        hits,
        limit=limit,
        sections_per_file=sections_per_file,
        score_threshold=sections_score_threshold,
    )

    if with_trace:
        trace = SearchTrace(
            query=query,
            intent=intent,
            regime=regime,
            strong_signal=ss_trace,
            fusion=fusion_trace,
            cascade=cascade_trace,
            elapsed_ms=0,  # populated by caller via timer; left 0 here for unit tests
        )
        return groups, trace
    return groups


def _evaluate_strong_signal(probe: list[Hit], *, intent_present: bool) -> StrongSignalTrace:
    """Decide whether the literal probe is a clear winner.

    A single uncontested hit (``len(probe) == 1``) treats the runner-up
    score as 0 — the gap then equals the top score, so any top above
    the score threshold fires. Mirrors QMD's behaviour where
    ``secondScore`` defaults to 0 when no runner-up exists.
    """
    top_n = normalise_bm25(probe[0].score) if probe else 0.0
    second_n = normalise_bm25(probe[1].score) if len(probe) > 1 else 0.0
    gap = top_n - second_n
    if intent_present or not probe:
        return StrongSignalTrace(
            top_score_norm=top_n,
            second_score_norm=second_n,
            gap_norm=gap,
            threshold_score=STRONG_SIGNAL_MIN_NORM_SCORE,
            threshold_gap=STRONG_SIGNAL_MIN_NORM_GAP,
            fired=False,
            disabled_by_intent=intent_present,
        )
    fired = top_n >= STRONG_SIGNAL_MIN_NORM_SCORE and gap >= STRONG_SIGNAL_MIN_NORM_GAP
    return StrongSignalTrace(
        top_score_norm=top_n,
        second_score_norm=second_n,
        gap_norm=gap,
        threshold_score=STRONG_SIGNAL_MIN_NORM_SCORE,
        threshold_gap=STRONG_SIGNAL_MIN_NORM_GAP,
        fired=fired,
        disabled_by_intent=False,
    )


def _cascade_regime_label(trace: CascadeTrace) -> str:
    """Format the cascade regime as ``cascade(+fuzzy)`` / ``cascade(+syn)``
    / ``cascade(+fuzzy+syn)`` based on which passes contributed."""
    suffixes: list[str] = []
    for p in trace.passes:
        if p.name == "fuzzy" and p.new_count > 0:
            suffixes.append("+fuzzy")
        elif p.name == "synonym" and p.new_count > 0:
            suffixes.append("+syn")
    return f"cascade({''.join(suffixes)})" if suffixes else "cascade"


def _empty_trace(query: str, intent: str | None) -> SearchTrace:
    return SearchTrace(
        query=query,
        intent=intent,
        regime="empty",
        strong_signal=StrongSignalTrace(
            top_score_norm=0.0,
            second_score_norm=0.0,
            gap_norm=0.0,
            threshold_score=STRONG_SIGNAL_MIN_NORM_SCORE,
            threshold_gap=STRONG_SIGNAL_MIN_NORM_GAP,
            fired=False,
            disabled_by_intent=False,
        ),
        fusion=None,
        cascade=None,
        elapsed_ms=0,
    )

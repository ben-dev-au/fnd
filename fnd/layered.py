"""Regime-aware layered search.

One entry point, :func:`search_layered`, for the TUI and the CLI. It chooses
one of three regimes:

* **strong-signal**: literal probe alone, when the normalised top BM25
  ≥ 0.85 AND gap ≥ 0.15 AND no intent provided.
  Bypasses fusion's phrase + syn passes entirely.
* **fusion**: phrase + lex + syn sub-queries, RRF-fused. Default.
* **cascade**: widening fallback when fusion's chunk pool is
  sparse (< limit / 4). Adds fuzzy~1 and synonym passes.

A query of two or more words orders its hits by :attr:`fnd.query.Hit.rank_score`:
the fused score, or list position on the same scale for strong-signal and
cascade. A one-word query has no phrase pass for fusion to weigh, so it orders by
each hit's best BM25 instead (judged eval, 180 queries: +0.013 nDCG@10 over fused
order, +0.011 over BM25 order, never worse on held-out queries). A ranking profile
scales whichever score orders.

The :class:`SearchTrace` returned when ``with_trace=True`` records which
regime fired and why.

Strong-signal bypass adapted from tobi/qmd (MIT); see README.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Literal, overload

from fnd.cascade import _with_pass, cascade_search
from fnd.explain import CascadeTrace, SearchTrace, StrongSignalTrace
from fnd.fusion import (
    STRONG_SIGNAL_MIN_NORM_GAP,
    STRONG_SIGNAL_MIN_NORM_SCORE,
    auto_subqueries,
    fusion_search,
    normalise_bm25,
    query_corrections,
    query_rare_spellings,
    rank_by_position,
)
from fnd.query import (
    CandidatePool,
    FileGroup,
    Hit,
    Searcher,
    SourceScope,
    candidate_window,
    file_key,
    group_by_file,
)
from fnd.render import keep_shown

if TYPE_CHECKING:
    from fnd.tag_query import TagFilter
from fnd.synonyms import SynonymTable
from fnd.typos import respelt


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
    source_scope: SourceScope | None = ...,
    intent: str | None = ...,
    profile: object | None = ...,
    now: int | None = ...,
    auto_fuzzy_enabled: bool = ...,
    min_term_chars: int = ...,
    tag_filter: TagFilter | None = ...,
    collapse_copies: bool = ...,
    as_typed: bool = ...,
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
    source_scope: SourceScope | None = ...,
    intent: str | None = ...,
    profile: object | None = ...,
    now: int | None = ...,
    auto_fuzzy_enabled: bool = ...,
    min_term_chars: int = ...,
    tag_filter: TagFilter | None = ...,
    collapse_copies: bool = ...,
    as_typed: bool = ...,
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
    source_scope: SourceScope | None = None,
    intent: str | None = None,
    profile: object | None = None,
    now: int | None = None,
    auto_fuzzy_enabled: bool = True,
    min_term_chars: int = 0,
    tag_filter: TagFilter | None = None,
    collapse_copies: bool = False,
    as_typed: bool = False,
    with_trace: bool = False,
) -> list[FileGroup] | tuple[list[FileGroup], SearchTrace]:
    """Run the regime-aware search and return ranked :class:`FileGroup`s.

    See module docstring for regime semantics. The probe doubles as
    fusion's lex sub-query when bypass does NOT fire, saving one
    Tantivy round-trip per non-bypass query. ``as_typed`` respells nothing
    (:mod:`fnd.typos`).
    """
    from fnd.query_plan import query_text, search_text

    query = search_text(query_text(query))
    typed = query
    if not query.strip() and metadata_filter:
        # A frontmatter rule alone lists the notes it admits.
        from fnd.file_facts import frontmatter_kinds

        typed = f"[{metadata_filter}]"
        query = f"kind:({' '.join(sorted(frontmatter_kinds()))})"
    if not query.strip():
        return ([], _empty_trace(query, intent)) if with_trace else []

    # Step 1: literal probe. Doubles as the bypass-decision input AND
    # (when bypass fires) the result set. When bypass does NOT fire,
    # fusion reuses this as its precomputed lex ranking — no wasted
    # Tantivy round-trip.
    window = candidate_window(limit)
    probe = searcher._candidates(
        query,
        window=window,
        collection=collection,
        metadata_filter=metadata_filter,
        source_scope=source_scope,
        tag_filter=tag_filter,
        min_files=limit,
    )

    # Step 2: strong-signal check, skipped with an intent (the obvious BM25 match
    # may not be what the caller wants) or a respelt word (the probe lacked it).
    respell = auto_fuzzy_enabled and not as_typed
    fixes = query_corrections(searcher, query) if respell else {}
    ss_trace = _evaluate_strong_signal(probe.hits, intent_present=bool(intent), respelt=bool(fixes))
    fusion_trace = None
    cascade_trace: CascadeTrace | None = None
    exhausted = [probe.exhausted]
    all_files = [probe.all_files]

    if ss_trace.fired:
        # The shortcut skips fusion, so the query's other spelling (see
        # ``compound_table``) is appended here, after the outright match.
        compound = _compound_hits(
            searcher,
            query,
            probe.hits,
            window=window,
            collection=collection,
            metadata_filter=metadata_filter,
            source_scope=source_scope,
            tag_filter=tag_filter,
            min_files=limit,
        )
        hits = rank_by_position(probe.hits + compound.hits)
        exhausted.append(compound.exhausted)
        all_files.append(compound.all_files)
        regime = "strong-signal"
    else:
        # Step 3: fusion (default).
        if with_trace:
            hits, fusion_trace = fusion_search(
                searcher,
                query=query,
                limit=limit,
                collection=collection,
                synonyms=synonyms,
                metadata_filter=metadata_filter,
                source_scope=source_scope,
                precomputed_lex=probe,
                tag_filter=tag_filter,
                corrections=fixes,
                with_trace=True,
            )
            exhausted.extend(sq.exhausted for sq in fusion_trace.subqueries)
            all_files.extend(sq.all_files for sq in fusion_trace.subqueries)
        else:
            hits = fusion_search(
                searcher,
                query=query,
                limit=limit,
                collection=collection,
                synonyms=synonyms,
                metadata_filter=metadata_filter,
                source_scope=source_scope,
                precomputed_lex=probe,
                tag_filter=tag_filter,
                corrections=fixes,
            )

        regime = "fusion"
        # Step 4: cascade fallback when fusion's chunk pool is sparse.
        if len(hits) < max(1, limit // 4):
            if with_trace:
                cascade_hits, cascade_trace = cascade_search(
                    searcher,
                    query=query,
                    threshold=window,
                    limit=limit,
                    collection=collection,
                    synonyms=synonyms,
                    metadata_filter=metadata_filter,
                    source_scope=source_scope,
                    tag_filter=tag_filter,
                    auto_fuzzy_enabled=auto_fuzzy_enabled,
                    min_term_chars=min_term_chars,
                    corrections=fixes,
                    with_trace=True,
                )
            else:
                cascade_hits = cascade_search(
                    searcher,
                    query=query,
                    threshold=window,
                    limit=limit,
                    collection=collection,
                    synonyms=synonyms,
                    metadata_filter=metadata_filter,
                    source_scope=source_scope,
                    tag_filter=tag_filter,
                    auto_fuzzy_enabled=auto_fuzzy_enabled,
                    min_term_chars=min_term_chars,
                    corrections=fixes,
                )
            if len(cascade_hits) > len(hits):
                hits = rank_by_position(cascade_hits)
                regime = _cascade_regime_label(cascade_trace) if cascade_trace else "cascade"
                if cascade_trace is not None:
                    exhausted = [p.exhausted for p in cascade_trace.passes]
                    all_files = [p.all_files for p in cascade_trace.passes]

    # Step 5: rerank + group, identical for every regime. Hits stay light; the
    # caller materialises the ones it shows.
    if len(query.split()) < 2 and not regime.startswith("cascade"):
        hits = by_best_bm25(hits)
    if profile is not None:
        from fnd.rerank import RankingProfile, rerank_hits

        assert isinstance(profile, RankingProfile)
        if not profile.is_identity:
            hits = rerank_hits(hits, profile=profile, query=query, now=now)

    # The cascade orders its passes; a respelling outranks its fuzzy and synonym rows.
    cut = len(hits)
    if regime.startswith("cascade"):
        cut = next((i for i, h in enumerate(hits) if h.pass_index != 0), cut)
    widened: dict[str, tuple[str, ...]] = {}
    distinct = {file_key(h, collapse_copies=collapse_copies) for h in hits[:cut]}
    if respell and len(distinct) < limit:
        widened = query_rare_spellings(searcher, query)
        fill = (
            _respelt_fill(
                searcher,
                respelt(query.split(), widened),
                hits[:cut],
                window=window,
                collection=collection,
                metadata_filter=metadata_filter,
                source_scope=source_scope,
                tag_filter=tag_filter,
                limit=limit,
            )
            if widened
            else []
        )
        if fill:
            filled = {(h.parent_id, h.chunk_seq) for h in fill}
            rest = [h for h in hits[cut:] if (h.parent_id, h.chunk_seq) not in filled]
            hits = hits[:cut] + fill + rest
            regime += "(+respelt)"
        else:
            widened = {}

    groups = group_by_file(
        hits,
        limit=limit,
        sections_per_file=sections_per_file,
        score_threshold=sections_score_threshold,
        collapse_copies=collapse_copies,
    )

    if with_trace:
        shown = {g.parent_id for g in groups}
        if fusion_trace is not None:
            # Every fused chunk has a contribution row; only the shown ones
            # can be explained, and the rest would bloat the JSON.
            shown_keys = {(h.parent_id, h.chunk_seq) for g in groups for h in g.hits}
            fusion_trace = dataclasses.replace(
                fusion_trace,
                contributions=[
                    c
                    for c in fusion_trace.contributions
                    if (c.parent_id, c.chunk_seq) in shown_keys
                ],
            )
        trace = SearchTrace(
            query=typed,
            intent=intent,
            regime=regime,
            # A pass pages until it holds more files than ``limit`` or has seen
            # every matching one; one that stopped at its ceiling left files unseen.
            files_truncated=(
                len({file_key(h, collapse_copies=collapse_copies) for h in hits}) > len(groups)
                or not all(all_files)
            ),
            # A pass that stopped paging left matching chunks unread, so a
            # shown file's section count is a floor.
            sections_truncated=(
                not all(exhausted)
                or sum(len(g.hits) for g in groups) < sum(1 for h in hits if h.parent_id in shown)
            ),
            strong_signal=ss_trace,
            fusion=fusion_trace,
            cascade=cascade_trace,
            elapsed_ms=0,  # populated by caller via timer; left 0 here for unit tests
            phrase_globs=_phrase_globs(searcher, query),
            widened=widened,
        )
        return groups, trace
    return groups


def _phrase_globs(searcher: Searcher, query: str) -> tuple[tuple[str, frozenset[str] | None], ...]:
    """The stems each pattern word of ``query``'s phrases and proximity groups searched."""
    if not any(ch in query for ch in "*?~"):
        return ()
    from fnd.matching import MatchSpec
    from fnd.query_resolvers import phrase_member_stems

    globs = MatchSpec.from_query(query, auto_fuzzy=False).phrase_patterns
    return tuple((g, phrase_member_stems(searcher, g)) for g in sorted(globs))


def _respelt_fill(
    searcher: Searcher,
    query: str,
    found: list[Hit],
    *,
    window: int,
    collection: str | list[str] | None,
    metadata_filter: str | None,
    source_scope: SourceScope | None,
    tag_filter: TagFilter | None,
    limit: int,
) -> list[Hit]:
    """``query``'s hits from files ``found`` lacks, to rank below every exact match."""
    pool = searcher._candidates(
        query,
        window=window,
        collection=collection,
        metadata_filter=metadata_filter,
        source_scope=source_scope,
        tag_filter=tag_filter,
        min_files=limit,
    )
    have = {h.parent_id for h in found}
    return [_with_pass(h, 1) for h in pool.hits if h.parent_id not in have]


def by_best_bm25(hits: list[Hit]) -> list[Hit]:
    """``hits`` ordered by their best BM25 (``Hit.score``), stable for ties."""
    return sorted((dataclasses.replace(h, rank_score=None) for h in hits), key=lambda h: -h.score)


def _compound_hits(
    searcher: Searcher,
    query: str,
    found: list[Hit],
    *,
    window: int,
    collection: str | list[str] | None,
    metadata_filter: str | None,
    source_scope: SourceScope | None,
    tag_filter: TagFilter | None,
    min_files: int,
) -> CandidatePool:
    """Hits of the query's hyphenated or joined spelling that ``found`` lacks."""
    subs = [s for s in auto_subqueries(query, synonyms=None) if s.source == "compound"]
    if not subs:
        return CandidatePool([], exhausted=True, all_files=True)
    pool = searcher._candidates(
        subs[0].query,
        window=window,
        collection=collection,
        metadata_filter=metadata_filter,
        source_scope=source_scope,
        tag_filter=tag_filter,
        min_files=min_files,
    )
    seen = {(h.parent_id, h.chunk_seq) for h in found}
    return CandidatePool(
        [
            dataclasses.replace(h, pass_index=1)
            for h in keep_shown(pool.hits, query)
            if (h.parent_id, h.chunk_seq) not in seen
        ],
        exhausted=pool.exhausted,
        all_files=pool.all_files,
    )


def _evaluate_strong_signal(
    probe: list[Hit], *, intent_present: bool, respelt: bool = False
) -> StrongSignalTrace:
    """Decide whether the literal probe is a clear winner.

    A single uncontested hit (``len(probe) == 1``) treats the runner-up
    score as 0 — the gap then equals the top score, so any top above
    the score threshold fires. Mirrors QMD's behaviour where
    ``secondScore`` defaults to 0 when no runner-up exists.
    """
    top_n = normalise_bm25(probe[0].score) if probe else 0.0
    second_n = normalise_bm25(probe[1].score) if len(probe) > 1 else 0.0
    gap = top_n - second_n
    if intent_present or respelt or not probe:
        return StrongSignalTrace(
            top_score_norm=top_n,
            second_score_norm=second_n,
            gap_norm=gap,
            threshold_score=STRONG_SIGNAL_MIN_NORM_SCORE,
            threshold_gap=STRONG_SIGNAL_MIN_NORM_GAP,
            fired=False,
            disabled_by_intent=intent_present,
            disabled_by_respelling=respelt,
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
        elif p.name == "compound" and p.new_count > 0:
            suffixes.append("+compound")
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

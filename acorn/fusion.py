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
"""

from __future__ import annotations

from dataclasses import dataclass

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
    if len(q.split()) >= 2:
        subs.append(SubQuery(query=f'"{q}"', weight=_DEFAULT_WEIGHTS["phrase"], source="phrase"))
    subs.append(SubQuery(query=q, weight=_DEFAULT_WEIGHTS["lex"], source="lex"))
    if synonyms is not None and synonyms.groups:
        expanded = expand(q, synonyms)
        if expanded != q:
            subs.append(SubQuery(query=expanded, weight=_DEFAULT_WEIGHTS["syn"], source="syn"))
    return subs


def parse_multi_input(text: str, *, synonyms: SynonymTable | None) -> list[SubQuery]:
    """Parse the ``:multi`` typed-input syntax into parallel sub-queries.

    Each non-blank line is ``<source>: <value>``. Recognised sources are
    ``lex``, ``phrase``, and ``syn``. Lines starting with ``#`` are
    comments. Unknown prefixes are ignored (the TUI surfaces a parse error
    inline; this function stays permissive so a typo in one line doesn't
    abort a usable multi-line query).

    A ``phrase:`` value that isn't already quoted is wrapped in quotes so
    the Tantivy parser sees it as a phrase query.
    A ``syn:`` value is expanded against the supplied table at parse time.
    """
    subs: list[SubQuery] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        prefix, value = line.split(":", 1)
        prefix = prefix.strip().lower()
        value = value.strip()
        if not value or prefix not in _DEFAULT_WEIGHTS:
            continue
        if prefix == "phrase" and not (value.startswith('"') and value.endswith('"')):
            value = f'"{value}"'
        elif prefix == "syn" and synonyms is not None and synonyms.groups:
            value = expand(value, synonyms)
        subs.append(SubQuery(query=value, weight=_DEFAULT_WEIGHTS[prefix], source=prefix))
    return subs


def fusion_search(
    searcher: Searcher,
    *,
    query: str,
    limit: int = 50,
    collection: str | None = None,
    synonyms: SynonymTable | None = None,
    subqueries: list[SubQuery] | None = None,
) -> list[Hit]:
    """Run sub-queries in parallel and RRF-fuse the results.

    When ``subqueries`` is None, calls :func:`auto_subqueries`. When
    explicit sub-queries are supplied (e.g. from a ``:multi`` panel),
    auto-derivation is skipped — only the supplied list runs.

    Each sub-query is issued through ``searcher._raw_hits`` so the index
    sees identical analyzer/field-boost configuration as the single-pass
    search path. Results are deduplicated by ``(parent_id, chunk_seq)``;
    ``pass_index`` is set from the highest-weighted contributing source.
    """
    subs = subqueries if subqueries is not None else auto_subqueries(query, synonyms=synonyms)
    if not subs:
        return []

    rankings: list[list[Hit]] = []
    for sub in subs:
        rankings.append(searcher._raw_hits(sub.query, limit=limit, collection=collection))

    # RRF dedup + sort. We rebuild the per-doc top-source map alongside the
    # fused score so we can attribute pass_index after the fact — rrf_fuse
    # itself is source-agnostic.
    weights = [s.weight for s in subs]
    fused = rrf_fuse(rankings, weights=weights)

    primary_source = _attribute_sources(rankings, subs)
    out: list[Hit] = []
    for h in fused[:limit]:
        key = (h.parent_id, h.chunk_seq)
        src = primary_source.get(key, "lex")
        out.append(_with_pass_index(h, _SOURCE_TO_PASS_INDEX.get(src, 0)))
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
        chunk_seq=h.chunk_seq,
        mtime=h.mtime,
        pass_index=pass_index,
        meta_blob=h.meta_blob,
    )

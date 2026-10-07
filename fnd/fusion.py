"""Parallel multi-query + Reciprocal Rank Fusion.

The cascade in :mod:`fnd.cascade` widens *sequentially*: only run the next
pass if the previous one came up short. This module runs sub-queries *in
parallel* and fuses them with Reciprocal Rank Fusion (RRF), so a doc that
ranks well in several sub-queries gets a real boost. The two mechanisms are
complementary; the search layer can use either.

Auto-derived sub-queries:

* ``phrase`` — the user's query wrapped in quotes (weight 2.0). Only emitted
  for multi-word queries (a phrase pass on a single word is identical to
  the lex pass and would just inflate the RRF score for everything).
* ``lex`` — the literal user query (weight 1.0). Implicit AND across terms.
* ``syn`` — synonym-expanded version of the query (weight 0.6). Only emitted
  when the expansion actually changes the query string.

A ``stem`` sub-query is omitted: the body field is already analysed with
``fnd_text`` (folded Snowball English), so an explicit stemmed pass would duplicate
the lex pass.

Pass-index attribution: each fused hit is tagged with ``pass_index``
matching the highest-weighted sub-query that surfaced it. This lets the
TUI render a per-source glyph using the same vocabulary as cascade
(``~`` fuzzy, ``⊕`` synonym) plus a new glyph for fusion-phrase hits
(``pass_index=3``).

Strong-signal bypass and the ``intent:`` line in
:func:`parse_multi_input` are adapted from `tobi/qmd
<https://github.com/tobi/qmd>`_ (MIT). The score normalization
``s / (1 + s)`` and the threshold values 0.85 / 0.15 come from QMD;
the implementation here is a Python rewrite. See README acknowledgments.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, overload

from fnd.explain import FusionTrace, HitContribution, SubQueryTrace
from fnd.query import CandidatePool, Hit, Searcher, SourceScope, candidate_window
from fnd.query_errors import QuerySyntaxError
from fnd.render import keep_shown
from fnd.synonyms import SynonymTable, compound_table, expand
from fnd.typos import corrections, respelt

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fnd.tag_query import TagFilter

# A field qualifier (``kind:pdf``, ``c:wine``) anywhere in the query — phrase
# wrapping such a query would quote the qualifier and produce a junk phrase.
_FIELD_SYNTAX_RE: Final = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*:")
# Explicit operator syntax — boolean keywords, ``+``/``-`` required/prohibited
# prefixes, wildcard/fuzzy chars, or a ``/regex/`` token. Quoting such a query as
# a phrase destroys its meaning (``"crypto* -wallet"`` becomes the phrase "crypto
# wallet" and re-admits the very doc the ``-`` excluded). The lex pass already
# honours it exactly via the boolean AST compiler, so the phrase pass stands down.
# ``/`` matches only as a delimited ``/regex/`` token, so plain slashes in paths
# or ``TCP/IP`` don't suppress the phrase pass. Parens and ``^`` (grouping /
# boost) also count as structure — ``(cross entropy)`` and ``foo^2`` already
# carry intent in the lex pass and shouldn't get an auto-phrase pass.
_OPERATOR_SYNTAX_RE: Final = re.compile(
    r"\b(?:AND|OR|NOT)\b|[*?~^()]|(?:^|\s)[+\-]\S|(?:^|\s)/[^/\s]+/(?:\s|$)"
)

# RRF constant; default 60 matches the original Cormack/Clarke/Buettcher 2009
# paper and what QMD uses.
_RRF_K_DEFAULT = 60


def _rrf_contribution(weight: float, rank: int, k: int = _RRF_K_DEFAULT) -> float:
    """One sub-query's contribution to a doc's fused score, ``weight / (k + rank)``.
    Shared by the fuser, the source attributor and the explain trace."""
    return weight / (k + rank)


def _ranked(hits: list[Hit]) -> Iterator[tuple[int, Hit]]:
    """``(rank, hit)`` in order, equal scores sharing a rank (1, 2, 2, 4), so a
    pass's tied sections stay tied after fusion and fall to document order."""
    rank = 0
    previous: float | None = None
    for i, h in enumerate(hits, start=1):
        if h.score != previous:
            rank, previous = i, h.score
        yield rank, h


def rank_by_position(hits: list[Hit]) -> list[Hit]:
    """``hits`` with ``rank_score`` set from list position, on fusion's scale."""
    return [dataclasses.replace(h, rank_score=_rrf_contribution(1.0, r)) for r, h in _ranked(hits)]


# Default per-source weights.
_DEFAULT_WEIGHTS: dict[str, float] = {
    "phrase": 2.0,
    "lex": 1.0,
    "syn": 0.6,
}

# The query the user meant: it must outrank the literal pass, which holds a word
# no document has. At the literal pass's weight the two only swap ranks and tie.
_TYPO_WEIGHT = 2.0

# Map source name → pass_index used by the TUI glyph table.
# Keep aligned with cascade: 0 = neutral (lex/exact), 1 = fuzzy,
# 2 = synonym, 3 = fusion-phrase.
_SOURCE_TO_PASS_INDEX: dict[str, int] = {
    "lex": 0,
    "fuzzy": 1,
    "compound": 1,
    "syn": 2,
    "phrase": 3,
    "typo": 1,
}

# Strong-signal bypass thresholds. Operate on a normalised BM25 score
# ``s_norm = s / (1 + s)``, monotone in [0, 1), so query-independent and
# corpus-stable. Adapted from tobi/qmd (MIT); see the README's Acknowledgments.
STRONG_SIGNAL_MIN_NORM_SCORE: float = 0.85
STRONG_SIGNAL_MIN_NORM_GAP: float = 0.15


def normalise_bm25(score: float) -> float:
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
    """Result of parsing a ``:multi`` block.

    ``intent`` does NOT produce a sub-query. It influences:

    * regime triage: intent disables strong-signal bypass
      (:func:`fnd.layered._evaluate_strong_signal`)
    * snippet selection: chunks containing intent tokens preferred, once a
      shown hit is materialised (:func:`fnd.query.materialise`)
    """

    subqueries: list[SubQuery]
    intent: str | None = None


def rrf_fuse(
    rankings: list[list[Hit]],
    *,
    weights: list[float],
    k: int = _RRF_K_DEFAULT,
) -> list[Hit]:
    """Fuse ranked lists with Reciprocal Rank Fusion, deduplicated by
    ``(parent_id, chunk_seq)`` and sorted by the fused score.

    A doc at rank ``r`` of a list with weight ``w`` earns ``w / (k + r)``; the
    sum is its ``rank_score``. ``score`` becomes its best BM25 across the lists.
    """
    if not rankings or not any(rankings):
        return []
    assert len(rankings) == len(weights), "weights must match rankings count"

    fused_score: dict[tuple[str, int], float] = {}
    best_bm25: dict[tuple[str, int], float] = {}
    representative: dict[tuple[str, int], Hit] = {}
    for ranking, weight in zip(rankings, weights, strict=True):
        for rank, hit in _ranked(ranking):
            key = (hit.parent_id, hit.chunk_seq)
            fused_score[key] = fused_score.get(key, 0.0) + _rrf_contribution(weight, rank, k)
            best_bm25[key] = max(hit.score, best_bm25.get(key, hit.score))
            representative.setdefault(key, hit)

    out = [
        dataclasses.replace(representative[key], score=best_bm25[key], rank_score=total)
        for key, total in fused_score.items()
    ]
    out.sort(key=lambda h: h.rank_key, reverse=True)
    return out


def auto_subqueries(query: str, *, synonyms: SynonymTable | None) -> list[SubQuery]:
    """Derive parallel sub-queries from a typed user query.

    Multi-word queries get a ``phrase`` + ``lex`` pair; single-word queries
    only get ``lex`` (a phrase of one word is identical to a term query
    and would just dilute the RRF math). A ``syn`` pass is appended only
    when synonym expansion actually rewrites the query — otherwise it
    would issue an identical Tantivy round-trip for nothing.

    The syn pass, like the phrase pass, stands down for any structured query
    (quotes / ``{N}`` / ``NEAR``, field qualifiers, or operators): ``expand``
    grafts an ``(a OR b)`` disjunction into the string, which strands a
    proximity brace (``{20}("a b" OR c)``) or mangles the operator the lex
    pass already honours. Tradeoff: ``kind:pdf threat intelligence`` no longer
    auto-expands ``ti`` — synonym widening only applies to plain bag-of-words
    queries.
    """
    q = query.strip()
    if not q:
        return []
    subs: list[SubQuery] = []
    # Skip the auto-phrase pass when the query already encodes phrase intent in
    # the lex pass: a user-supplied quote (Tantivy parses ``"a b c"`` as a
    # PhraseQuery directly) or proximity (``{N} …`` / ``a NEAR/N b`` expand to
    # ``"a b"~N`` downstream). Re-wrapping either would double-quote — ``""a b""``
    # or ``""a b"~N`` — and crash the parser. Also skip when the query carries a
    # field qualifier (``kind:pdf``, ``c:wine``): a phrase over the raw qualifier
    # text is meaningless and quoting it mangles the qualifier.
    carries_field_syntax = bool(_FIELD_SYNTAX_RE.search(q))
    carries_operator_syntax = bool(_OPERATOR_SYNTAX_RE.search(q))
    if len(q.split()) >= 2 and _is_bag_of_words(q):
        subs.append(SubQuery(query=f'"{q}"', weight=_DEFAULT_WEIGHTS["phrase"], source="phrase"))
    subs.append(SubQuery(query=q, weight=_DEFAULT_WEIGHTS["lex"], source="lex"))
    if synonyms is not None and synonyms.groups and _is_bag_of_words(q):
        expanded = expand(q, synonyms)
        if expanded != q:
            subs.append(SubQuery(query=expanded, weight=_DEFAULT_WEIGHTS["syn"], source="syn"))
    # The tokenizer splits "drop-down", so a one-word query's other spelling needs
    # its own pass here too: the cascade's copy runs only when these find almost
    # nothing. Longer queries keep the cascade's: rewriting each word loosens them.
    if len(q.split()) == 1 and not carries_field_syntax and not carries_operator_syntax:
        # The other spellings only: with the literal in the disjunction, its hits
        # can fill the pass's bounded result list and leave them no slot.
        literal = q.replace("-", " ").lower()
        others = [
            f'"{m}"' if " " in m else m
            for group in compound_table(q).groups
            for m in group
            if m.lower() != literal
        ]
        if others:
            subs.append(
                SubQuery(
                    query=" OR ".join(others), weight=_DEFAULT_WEIGHTS["syn"], source="compound"
                )
            )
    return subs


def _is_bag_of_words(query: str) -> bool:
    """No phrase, proximity, field qualifier or operator: plain words only."""
    return not (
        '"' in query
        or "{" in query
        or "NEAR/" in query
        or _FIELD_SYNTAX_RE.search(query)
        or _OPERATOR_SYNTAX_RE.search(query)
    )


def query_corrections(searcher: Searcher, query: str) -> dict[str, tuple[str, ...]]:
    """Respellings for a plain-words query's unindexed words (see :mod:`fnd.typos`)."""
    return corrections(searcher, query.split()) if _is_bag_of_words(query) else {}


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
    pass it to :func:`fnd.layered.search_layered`. Last-write-wins if
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
    collection: str | list[str] | None = ...,
    synonyms: SynonymTable | None = ...,
    subqueries: list[SubQuery] | None = ...,
    metadata_filter: str | None = ...,
    source_scope: SourceScope | None = ...,
    precomputed_lex: CandidatePool | None = ...,
    tag_filter: TagFilter | None = ...,
    corrections: dict[str, tuple[str, ...]] | None = ...,
    with_trace: Literal[False] = False,
) -> list[Hit]: ...


@overload
def fusion_search(
    searcher: Searcher,
    *,
    query: str,
    limit: int = ...,
    collection: str | list[str] | None = ...,
    synonyms: SynonymTable | None = ...,
    subqueries: list[SubQuery] | None = ...,
    metadata_filter: str | None = ...,
    source_scope: SourceScope | None = ...,
    precomputed_lex: CandidatePool | None = ...,
    tag_filter: TagFilter | None = ...,
    corrections: dict[str, tuple[str, ...]] | None = ...,
    with_trace: Literal[True],
) -> tuple[list[Hit], FusionTrace]: ...


def fusion_search(
    searcher: Searcher,
    *,
    query: str,
    limit: int = 50,
    collection: str | list[str] | None = None,
    synonyms: SynonymTable | None = None,
    subqueries: list[SubQuery] | None = None,
    metadata_filter: str | None = None,
    source_scope: SourceScope | None = None,
    precomputed_lex: CandidatePool | None = None,
    tag_filter: TagFilter | None = None,
    corrections: dict[str, tuple[str, ...]] | None = None,
    with_trace: bool = False,
) -> list[Hit] | tuple[list[Hit], FusionTrace]:
    """Run sub-queries in parallel and RRF-fuse the results.

    When ``subqueries`` is None, calls :func:`auto_subqueries`. When
    explicit sub-queries are supplied (e.g. from a ``:multi`` panel),
    auto-derivation is skipped — only the supplied list runs.

    ``limit`` is the number of FILES the caller will show. Each sub-query
    pages through its matches until it holds more distinct files than that
    (see :meth:`fnd.query.Searcher._candidates`), and every fused chunk is
    returned, so the file-level cut happens in the caller's grouper and a
    book with many matching chunks cannot push other files out first. Hits
    are light; the caller materialises the ones it shows.

    Each sub-query applies the metadata filter and the ``source_path`` scope.
    Results are deduplicated by ``(parent_id, chunk_seq)`` and sorted by RRF
    position; ``pass_index`` is set from the highest-weighted contributing
    source.

    **Score semantics**: ``Hit.rank_score`` is the fused score the list is
    ordered by; ``Hit.score`` is the best BM25 across the sub-queries, the
    number the results pane shows.

    ``precomputed_lex``: when supplied, the lex sub-query reuses this pool
    instead of issuing a fresh one. Lets the regime probe in
    :mod:`fnd.layered` double as fusion's lex pass, saving one Tantivy
    round-trip on every non-bypass query.

    ``with_trace``: when ``True``, returns ``(hits, FusionTrace)`` so
    callers (CLI ``--explain`` / TUI ``:explain``) can inspect which
    sub-queries ran, per-hit BM25 scores, RRF contributions, and
    primary-source attribution. Default ``False`` returns the existing
    ``list[Hit]`` unchanged.
    """
    subs = subqueries if subqueries is not None else auto_subqueries(query, synonyms=synonyms)
    fixes: dict[str, tuple[str, ...]] = {}
    if subqueries is None:
        fixes = query_corrections(searcher, query) if corrections is None else corrections
        if fixes:
            subs = [*subs, SubQuery(respelt(query.split(), fixes), _TYPO_WEIGHT, "typo")]
    if not subs:
        if with_trace:
            return [], _empty_fusion_trace(query)
        return []

    def _issue(q: str) -> CandidatePool:
        return searcher._candidates(
            q,
            window=candidate_window(limit),
            collection=collection,
            metadata_filter=metadata_filter,
            source_scope=source_scope,
            tag_filter=tag_filter,
            min_files=limit,
        )

    rankings: list[list[Hit]] = []
    degraded: list[bool] = []
    exhausted: list[bool] = []
    all_files: list[bool] = []
    for sub in subs:
        if sub.source == "lex":
            # The lex pass carries the user's literal query: reuse the regime
            # probe's result when supplied, else issue it. A syntax error here is
            # the user's to fix, so it must propagate — the Searcher safety-net
            # and the TUI's inline notice both rely on malformed queries raising.
            pool = precomputed_lex if precomputed_lex is not None else _issue(sub.query)
            rankings.append(pool.hits)
            degraded.append(False)
            exhausted.append(pool.exhausted)
            all_files.append(pool.all_files)
            continue
        # Auto-derived passes (phrase / syn): a malformed sub-query degrades to
        # an empty ranking rather than aborting the whole fused search.
        try:
            pool = _issue(sub.query)
            rankings.append(keep_shown(pool.hits, query) if sub.source == "compound" else pool.hits)
            degraded.append(False)
            exhausted.append(pool.exhausted)
            all_files.append(pool.all_files)
        except QuerySyntaxError:
            rankings.append([])
            degraded.append(True)
            exhausted.append(True)
            all_files.append(True)

    weights = [s.weight for s in subs]
    fused = rrf_fuse(rankings, weights=weights)

    primary_source = _attribute_sources(rankings, subs)
    out = [
        _with_pass_index(
            h, _SOURCE_TO_PASS_INDEX.get(primary_source.get((h.parent_id, h.chunk_seq), "lex"), 0)
        )
        for h in fused
    ]

    if not with_trace:
        return out
    trace = _build_fusion_trace(
        query, subs, rankings, degraded, exhausted, all_files, primary_source, out, fixes
    )
    return out, trace


def _build_fusion_trace(
    query: str,
    subs: list[SubQuery],
    rankings: list[list[Hit]],
    degraded: list[bool],
    exhausted: list[bool],
    all_files: list[bool],
    primary_source: dict[tuple[str, int], str],
    out: list[Hit],
    fixes: dict[str, tuple[str, ...]],
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
            degraded=d,
            exhausted=e,
            all_files=f,
        )
        for s, r, d, e, f in zip(subs, rankings, degraded, exhausted, all_files, strict=True)
    ]
    # Per-hit contributions: walk each ranking once, accumulate
    # rank/bm25/rrf for each (parent_id, chunk_seq) appearing in ``out``.
    out_keys = {(h.parent_id, h.chunk_seq) for h in out}
    bm25_per: dict[tuple[str, int], dict[str, float]] = {k: {} for k in out_keys}
    rank_per: dict[tuple[str, int], dict[str, int]] = {k: {} for k in out_keys}
    rrf_per: dict[tuple[str, int], dict[str, float]] = {k: {} for k in out_keys}
    for ranking, sub in zip(rankings, subs, strict=True):
        for rank, h in _ranked(ranking):
            key = (h.parent_id, h.chunk_seq)
            if key not in bm25_per:
                continue
            rank_per[key][sub.source] = rank
            bm25_per[key][sub.source] = h.score
            rrf_per[key][sub.source] = _rrf_contribution(sub.weight, rank)

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
        default_weights=dict(_DEFAULT_WEIGHTS),
        corrections=fixes,
    )


def _empty_fusion_trace(query: str) -> FusionTrace:
    return FusionTrace(
        query=query,
        subqueries=[],
        contributions=[],
        rrf_k=_RRF_K_DEFAULT,
        default_weights=dict(_DEFAULT_WEIGHTS),
    )


def _attribute_sources(
    rankings: list[list[Hit]], subs: list[SubQuery]
) -> dict[tuple[str, int], str]:
    """Each doc's primary source: ``phrase`` whenever the phrase pass found it,
    else the sub-query that ranked it highest, the heavier one on a tie. By
    contribution alone, a synonym-led doc matching one literal word reads as lex."""
    primary_source: dict[tuple[str, int], str] = {}
    best: dict[tuple[str, int], tuple[bool, int, float]] = {}
    for ranking, sub in zip(rankings, subs, strict=True):
        for rank, hit in _ranked(ranking):
            key = (hit.parent_id, hit.chunk_seq)
            candidate = (sub.source != "phrase", rank, -sub.weight)
            if key not in best or candidate < best[key]:
                best[key] = candidate
                primary_source[key] = sub.source
    return primary_source


def _with_pass_index(h: Hit, pass_index: int) -> Hit:
    """``h`` tagged with the pass that produced it.

    A replace, not an enumerated rebuild: a hand-listed rebuild silently
    defaults any field it forgets, as ``line`` and then ``body_md`` once were.
    """
    return dataclasses.replace(h, pass_index=pass_index)

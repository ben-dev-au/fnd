"""Trace data structures for the ``--explain`` CLI flag and ``:explain``
TUI overlay.

Off the hot path: trace objects are only built when ``with_trace=True``
flows through :func:`fnd.layered.search_layered`. The data inside is
the same data that would otherwise be discarded after fusion / cascade
ran — no extra computation, just retention of internal state.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True, frozen=True)
class StrongSignalTrace:
    """Bypass-decision context (UX-pass-4 §1).

    ``top_score_norm`` / ``second_score_norm`` are the normalized BM25
    scores ``s / (1 + s)`` of the top-2 literal-probe hits. ``gap_norm``
    is the normalized gap between them. ``fired`` is true when both
    threshold checks passed AND no intent was supplied.
    """

    top_score_norm: float
    second_score_norm: float
    gap_norm: float
    threshold_score: float
    threshold_gap: float
    fired: bool
    disabled_by_intent: bool


@dataclass(slots=True, frozen=True)
class SubQueryTrace:
    """One sub-query that ran inside fusion."""

    source: str  # "phrase" | "lex" | "syn" | "fuzzy"
    query: str
    weight: float
    hit_count: int
    bm25_top: float  # raw BM25 of top hit in this sub-ranking (0.0 if empty)
    bm25_second: float
    rrf_k: int


@dataclass(slots=True, frozen=True)
class HitContribution:
    """Per-doc score breakdown across fusion's sub-queries."""

    parent_id: str
    chunk_seq: int
    bm25_per_source: dict[str, float]  # raw BM25 keyed by source name
    rank_per_source: dict[str, int]  # 1-indexed rank in each sub-query; 0 = absent
    rrf_per_source: dict[str, float]  # weight / (k + rank) + position bonus
    fused_total: float  # sum of rrf_per_source
    primary_source: str
    final_score: float  # the BM25 score restored to Hit.score


@dataclass(slots=True, frozen=True)
class FusionTrace:
    query: str
    subqueries: list[SubQueryTrace]
    contributions: list[HitContribution]  # ordered as fusion returned them
    rrf_k: int
    pos_bonus_rank_1: float
    pos_bonus_rank_2_3: float
    default_weights: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class CascadePassTrace:
    pass_index: int  # 0=literal, 1=fuzzy, 2=synonym
    name: str  # "literal" | "fuzzy" | "synonym"
    query: str  # the actual query string issued
    hit_count: int  # hits this pass returned
    new_count: int  # hits added after dedup against earlier passes
    bm25_top: float


@dataclass(slots=True, frozen=True)
class CascadeTrace:
    query: str
    passes: list[CascadePassTrace]
    threshold: int
    final_count: int


@dataclass(slots=True, frozen=True)
class SearchTrace:
    """Unified per-query trace surfaced by :func:`fnd.layered.search_layered`.

    ``regime`` is a short tag describing which path ran: ``strong-signal``,
    ``fusion``, ``cascade``, or ``cascade(+fuzzy)`` / ``cascade(+syn)`` /
    ``cascade(+fuzzy+syn)`` when widening passes contributed.
    """

    query: str
    intent: str | None
    regime: str
    strong_signal: StrongSignalTrace
    fusion: FusionTrace | None
    cascade: CascadeTrace | None
    elapsed_ms: int

    def to_json(self) -> dict[str, object]:
        return {
            "query": self.query,
            "intent": self.intent,
            "regime": self.regime,
            "strong_signal": _strong_signal_to_json(self.strong_signal),
            "fusion": _fusion_to_json(self.fusion) if self.fusion else None,
            "cascade": _cascade_to_json(self.cascade) if self.cascade else None,
            "elapsed_ms": self.elapsed_ms,
        }


def _strong_signal_to_json(t: StrongSignalTrace) -> dict[str, object]:
    return {
        "top_score_norm": round(t.top_score_norm, 4),
        "second_score_norm": round(t.second_score_norm, 4),
        "gap_norm": round(t.gap_norm, 4),
        "threshold_score": t.threshold_score,
        "threshold_gap": t.threshold_gap,
        "fired": t.fired,
        "disabled_by_intent": t.disabled_by_intent,
    }


def _fusion_to_json(t: FusionTrace) -> dict[str, object]:
    return {
        "query": t.query,
        "rrf_k": t.rrf_k,
        "pos_bonuses": {"rank_1": t.pos_bonus_rank_1, "rank_2_3": t.pos_bonus_rank_2_3},
        "default_weights": t.default_weights,
        "subqueries": [
            {
                "source": s.source,
                "query": s.query,
                "weight": s.weight,
                "hit_count": s.hit_count,
                "bm25_top": round(s.bm25_top, 4),
                "bm25_second": round(s.bm25_second, 4),
                "rrf_k": s.rrf_k,
            }
            for s in t.subqueries
        ],
        "contributions": [
            {
                "parent_id": c.parent_id,
                "chunk_seq": c.chunk_seq,
                "bm25_per_source": {k: round(v, 4) for k, v in c.bm25_per_source.items()},
                "rank_per_source": dict(c.rank_per_source),
                "rrf_per_source": {k: round(v, 6) for k, v in c.rrf_per_source.items()},
                "fused_total": round(c.fused_total, 6),
                "primary_source": c.primary_source,
                "final_score": round(c.final_score, 4),
            }
            for c in t.contributions
        ],
    }


def _cascade_to_json(t: CascadeTrace) -> dict[str, object]:
    return {
        "query": t.query,
        "threshold": t.threshold,
        "final_count": t.final_count,
        "passes": [
            {
                "pass_index": p.pass_index,
                "name": p.name,
                "query": p.query,
                "hit_count": p.hit_count,
                "new_count": p.new_count,
                "bm25_top": round(p.bm25_top, 4),
            }
            for p in t.passes
        ],
    }

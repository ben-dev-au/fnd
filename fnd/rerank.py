"""Python post-rank score adjustments.

Tantivy hardcodes BM25 ``k1``/``b`` upstream and exposes no per-doc score
callback. The high-leverage knobs (recency decay, per-kind weighting,
query-term clustering) scale each hit's :attr:`fnd.query.Hit.rank_key` here,
after retrieval, and the list is re-sorted; the display BM25 is left alone.

Design notes:

* Recency uses **exponential decay with half-life**, not Tantivy's fixed
  ``log2(2 + x)`` formula (`weight_by_field`), so the shape is configurable
  per profile and the closed-form math is unit-testable.
* Phrase proximity uses **stem equality** (Snowball English) so it agrees
  with how the index tokenizes; otherwise a query for "penfold" would miss
  the "penfolds" in the body and never form a window.
* Filetype boost is a flat multiplier per :attr:`Hit.kind`; absent kinds
  are neutral (multiplier 1.0).

``RankingProfile()`` (all zeros / empty) is the identity.
"""

from __future__ import annotations

import dataclasses
import math
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fnd.query import Hit, decoded_body
from fnd.render import _stem  # stem helper kept centralized in render.py

if TYPE_CHECKING:
    from fnd.config import Config


@dataclass(slots=True, frozen=True)
class RankingProfile:
    """Per-profile knobs.

    All zeros / empty maps are the identity — passing :class:`RankingProfile`
    with no args leaves the BM25 order untouched.
    """

    # Recency: ``score *= 1 + recency_boost * exp(-Δt / half_life)``.
    # ``recency_boost`` is the maximum bonus (0 disables). ``half_life`` is
    # in seconds.
    recency_boost: float = 0.0
    recency_half_life_seconds: int = 365 * 86_400

    # Per-kind multiplier; missing kinds are neutral.
    filetype_boosts: dict[str, float] = field(default_factory=dict)

    # Phrase proximity: extra score for hits whose distinct query terms
    # cluster within a small window. ``phrase_proximity`` is the maximum
    # bonus magnitude (0 disables); the curve maps a tight window to ~1x
    # boost and a window > ``proximity_max_window`` tokens to 0x boost.
    phrase_proximity: float = 0.0
    proximity_max_window: int = 50

    @property
    def is_identity(self) -> bool:
        """True when no adjustment is enabled."""
        return (
            self.recency_boost == 0.0 and not self.filetype_boosts and self.phrase_proximity == 0.0
        )


# ── pure adjustment functions ─────────────────────────────────────


def apply_recency_boost(*, score: float, mtime: int, profile: RankingProfile, now: int) -> float:
    """Multiply ``score`` by an exponential-decay recency factor.

    Formula::

        bonus = exp(- max(now - mtime, 0) / half_life)
        score' = score * (1 + recency_boost * bonus)

    Notes:

    * ``recency_boost = 0`` short-circuits to the identity (no FP drift).
    * Future-dated ``mtime`` (clock skew) is clamped at ``now`` so the
      bonus is bounded by the configured maximum.
    * ``mtime = 0`` (unknown) effectively returns the base score because
      the decay over ``now`` seconds is ~0 — neutral, not penalising.
    """
    if profile.recency_boost == 0.0:
        return score
    delta = max(now - mtime, 0)
    half = max(profile.recency_half_life_seconds, 1)
    bonus = math.exp(-delta * math.log(2) / half)
    return score * (1.0 + profile.recency_boost * bonus)


def apply_filetype_boost(*, score: float, kind: str, profile: RankingProfile) -> float:
    """Multiply ``score`` by ``profile.filetype_boosts[kind]`` (1.0 if absent)."""
    if not profile.filetype_boosts:
        return score
    return score * profile.filetype_boosts.get(kind, 1.0)


def apply_phrase_proximity(
    *, score: float, body: str, terms: list[str], profile: RankingProfile
) -> float:
    """Reward score when distinct query terms appear in a tight window.

    Implementation: tokenize ``body`` into word positions, locate every
    occurrence of every distinct term (stem-aware), and find the smallest
    contiguous window covering at least one occurrence of every term.
    Map that window size onto ``[0, profile.phrase_proximity]``.

    Multi-term queries only — single-term queries get neutral 0 bonus
    because there's nothing to cluster.
    """
    if profile.phrase_proximity == 0.0:
        return score
    distinct_stems = {_stem(t) for t in terms if t}
    if len(distinct_stems) < 2:
        return score

    positions: dict[str, list[int]] = {s: [] for s in distinct_stems}
    for i, m in enumerate(re.finditer(r"\w+", body)):
        st = _stem(m.group(0))
        if st in positions:
            positions[st].append(i)

    if any(not v for v in positions.values()):
        # A required term doesn't appear in the body — no window possible.
        return score

    window = _smallest_covering_window(positions)
    max_w = max(profile.proximity_max_window, 2)
    # Tight window (size == len(terms)) → factor ≈ 1; window >= max_w → 0.
    span = max(window - len(distinct_stems) + 1, 1)
    factor = max(0.0, 1.0 - math.log(span) / math.log(max_w))
    return score * (1.0 + profile.phrase_proximity * factor)


def _smallest_covering_window(positions: dict[str, list[int]]) -> int:
    """Return the smallest window (token count, inclusive) covering every key.

    Standard sliding-window over a merged sorted (pos, key) stream. Assumes
    every list is non-empty (caller guards).
    """
    merged: list[tuple[int, str]] = []
    for key, lst in positions.items():
        for p in lst:
            merged.append((p, key))
    merged.sort(key=lambda t: t[0])

    need = len(positions)
    have: dict[str, int] = {}
    have_count = 0
    best = merged[-1][0] - merged[0][0] + 1
    left = 0
    for right in range(len(merged)):
        rp, rk = merged[right]
        prev = have.get(rk, 0)
        have[rk] = prev + 1
        if prev == 0:
            have_count += 1
        while have_count == need:
            lp, lk = merged[left]
            best = min(best, rp - lp + 1)
            have[lk] -= 1
            if have[lk] == 0:
                have_count -= 1
            left += 1
    return best


# ── orchestrator ──────────────────────────────────────────────────


def rerank_hits(
    hits: list[Hit],
    *,
    profile: RankingProfile,
    query: str,
    now: int | None = None,
) -> list[Hit]:
    """``hits`` with each ``rank_score`` scaled by every enabled adjustment,
    re-sorted on it (stable for ties); ``score`` is unchanged."""
    now_ts = int(now) if now is not None else int(time.time())
    terms = _terms_for_proximity(query) if profile.phrase_proximity else []

    out: list[Hit] = []
    for h in hits:
        s = h.rank_key
        s = apply_recency_boost(score=s, mtime=h.mtime, profile=profile, now=now_ts)
        s = apply_filetype_boost(score=s, kind=h.kind, profile=profile)
        if terms:
            # Prefer the full decoded body; fall back to the snippet for
            # hits that predate body_text plumbing (e.g. external callers).
            s = apply_phrase_proximity(
                score=s, body=decoded_body(h)[0] or h.snippet, terms=terms, profile=profile
            )
        out.append(_with_rank_score(h, s))
    out.sort(key=lambda x: x.rank_key, reverse=True)
    return out


def _with_rank_score(h: Hit, rank_score: float) -> Hit:
    """``h`` with a new rank score; see :func:`fnd.fusion._with_pass_index` on why
    this is a ``dataclasses.replace``."""
    return dataclasses.replace(h, rank_score=rank_score)


def _terms_for_proximity(query: str) -> list[str]:
    """Strip operators / field qualifiers; keep only word terms.

    Reuses :func:`fnd.render._terms_from_query` semantics so highlight,
    search, and proximity all agree on what 'a query term' means.
    """
    from fnd.render import _terms_from_query

    return _terms_from_query(query)


def profile_from_config(cfg: object) -> RankingProfile:
    """Build a runtime :class:`RankingProfile` from the user-facing
    :class:`fnd.config.RankingProfileConfig`.

    Kept as a free function (rather than a method on the config model) so
    ``fnd.rerank`` stays importable without pulling pydantic in.
    """
    from fnd.config import RankingProfileConfig, parse_duration_seconds

    assert isinstance(cfg, RankingProfileConfig)
    return RankingProfile(
        recency_boost=cfg.recency_boost,
        recency_half_life_seconds=parse_duration_seconds(cfg.recency_half_life),
        filetype_boosts=dict(cfg.filetype_boosts),
        phrase_proximity=cfg.phrase_proximity,
        proximity_max_window=cfg.proximity_max_window,
    )


def profile_for_scope(cfg: Config | None, collections: Sequence[str] | None) -> RankingProfile:
    """The one scoped collection's ``ranking_profile``, else ``default``, else
    the identity. Shared by the TUI and ``fnd search``."""
    if cfg is None:
        return RankingProfile()
    name = "default"
    if collections is not None and len(collections) == 1:
        try:
            name = cfg.collection(collections[0]).ranking_profile or "default"
        except KeyError:
            name = "default"
    return profile_from_config(cfg.ranking_profile(name))

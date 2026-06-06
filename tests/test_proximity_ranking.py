"""Proximity ranking: terms close together rank above scattered ones,
measured over the full chunk body, and enabled by default.
"""

from __future__ import annotations

from fnd.config import RankingProfileConfig
from fnd.query import Hit
from fnd.rerank import RankingProfile, profile_from_config, rerank_hits


def _hit(pid: str, score: float, body_text: str) -> Hit:
    return Hit(
        score=score,
        parent_id=pid,
        path=f"/{pid}.md",
        kind="md",
        page=0,
        slide=0,
        heading_path="",
        title="",
        snippet="",  # deliberately empty: proximity must read body_text
        body_text=body_text,
    )


def test_proximity_promotes_tight_cluster_over_scattered() -> None:
    profile = RankingProfile(phrase_proximity=0.5, proximity_max_window=50)
    far = "alpha " + "x " * 60 + "beta " + "y " * 60 + "gamma"
    tight = "alpha beta gamma"
    hits = [_hit("far", 10.0, far), _hit("tight", 10.0, tight)]
    ranked = rerank_hits(hits, profile=profile, query="alpha beta gamma")
    assert ranked[0].parent_id == "tight"
    assert ranked[0].score > ranked[1].score


def test_proximity_measures_full_body_not_just_snippet() -> None:
    """Terms outside a (here empty) snippet still earn the boost."""
    profile = RankingProfile(phrase_proximity=0.5, proximity_max_window=50)
    h = _hit("x", 10.0, "alpha beta gamma")
    ranked = rerank_hits(h_list := [h], profile=profile, query="alpha beta gamma")
    assert ranked[0].score > 10.0
    assert h_list  # sanity


def test_proximity_multiplier_off_by_default() -> None:
    """The post-rank multiplier is opt-in; the default proximity signal is
    fusion's native sloppy-phrase passes (see test_phase_9_fusion)."""
    assert RankingProfileConfig().phrase_proximity == 0.0
    assert profile_from_config(RankingProfileConfig()).phrase_proximity == 0.0

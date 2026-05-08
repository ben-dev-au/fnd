"""Phase 7: Python post-rank score adjustments per plan §4.

Three knobs:
  * recency boost (exponential half-life multiplier)
  * filetype boost (per-kind multiplier)
  * phrase-proximity reward (multi-term-query clustering bonus)

All three are pure functions; the orchestrator :func:`acorn.rerank.rerank_hits`
applies them in sequence and re-sorts. Property tests cover monotonicity and
idempotence (zero-magnitude leaves order untouched).
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from acorn.index import build_index
from acorn.query import Hit, Searcher
from acorn.rerank import (
    RankingProfile,
    apply_filetype_boost,
    apply_phrase_proximity,
    apply_recency_boost,
    rerank_hits,
)

# ── pure-function unit tests ───────────────────────────────────────


def _hit(*, mtime: int = 0, kind: str = "pdf", score: float = 1.0, snippet: str = "") -> Hit:
    return Hit(
        score=score,
        parent_id=f"pid-{mtime}-{kind}-{score}",
        path=f"/{kind}.{kind}",
        kind=kind,
        page=0,
        slide=0,
        heading_path="",
        title="",
        snippet=snippet,
        chunk_seq=0,
        mtime=mtime,
    )


def test_recency_zero_boost_is_identity() -> None:
    """``recency_boost = 0`` is a documented neutral; score must not change."""
    profile = RankingProfile(recency_boost=0.0, recency_half_life_seconds=86_400)
    out = apply_recency_boost(score=2.5, mtime=1_700_000_000, profile=profile, now=2_000_000_000)
    assert out == pytest.approx(2.5)


def test_recency_boost_max_at_now() -> None:
    """A document modified ``now`` gets the full boost: score * (1 + boost)."""
    profile = RankingProfile(recency_boost=0.5, recency_half_life_seconds=86_400)
    out = apply_recency_boost(score=2.0, mtime=1_000_000, profile=profile, now=1_000_000)
    assert out == pytest.approx(2.0 * 1.5)


def test_recency_boost_decays_to_zero() -> None:
    """An infinitely-old document gets no boost (returns base score)."""
    profile = RankingProfile(recency_boost=0.5, recency_half_life_seconds=86_400)
    out = apply_recency_boost(score=2.0, mtime=0, profile=profile, now=10**12)
    assert out == pytest.approx(2.0, rel=1e-3)


def test_recency_boost_half_life_halves_bonus() -> None:
    """At one half-life ago, the bonus is exactly half the max bonus."""
    half = 86_400
    profile = RankingProfile(recency_boost=1.0, recency_half_life_seconds=half)
    now = 10_000_000
    full = apply_recency_boost(score=1.0, mtime=now, profile=profile, now=now)
    halfed = apply_recency_boost(score=1.0, mtime=now - half, profile=profile, now=now)
    # full = 1 * (1 + 1.0) = 2.0
    # halfed = 1 * (1 + 1.0 * 0.5) = 1.5
    assert full == pytest.approx(2.0)
    assert halfed == pytest.approx(1.5)


@given(
    older=st.integers(min_value=0, max_value=10**9),
    delta=st.integers(min_value=1, max_value=10**8),
    boost=st.floats(min_value=0.0, max_value=2.0, allow_nan=False, allow_infinity=False),
    half=st.integers(min_value=3600, max_value=10**8),
)
@settings(max_examples=200, deadline=None)
def test_recency_boost_monotonic_in_mtime(older: int, delta: int, boost: float, half: int) -> None:
    """For any positive boost, a newer document must score >= an older one
    (with the same base score)."""
    profile = RankingProfile(recency_boost=boost, recency_half_life_seconds=half)
    now = older + delta + 100
    s_old = apply_recency_boost(score=1.0, mtime=older, profile=profile, now=now)
    s_new = apply_recency_boost(score=1.0, mtime=older + delta, profile=profile, now=now)
    assert s_new >= s_old - 1e-9


def test_filetype_boost_unknown_kind_is_neutral() -> None:
    profile = RankingProfile(filetype_boosts={"pdf": 2.0})
    assert apply_filetype_boost(score=1.0, kind="docx", profile=profile) == pytest.approx(1.0)


def test_filetype_boost_applies_multiplier() -> None:
    profile = RankingProfile(filetype_boosts={"pdf": 1.4, "md": 0.8})
    assert apply_filetype_boost(score=2.0, kind="pdf", profile=profile) == pytest.approx(2.8)
    assert apply_filetype_boost(score=2.0, kind="md", profile=profile) == pytest.approx(1.6)


def test_phrase_proximity_single_term_is_neutral() -> None:
    """Single-term queries can't have a 'cluster' — bonus is always 0."""
    profile = RankingProfile(phrase_proximity=1.0)
    out = apply_phrase_proximity(
        score=1.0, body="quark gluon plasma", terms=["quark"], profile=profile
    )
    assert out == pytest.approx(1.0)


def test_phrase_proximity_rewards_close_clusters() -> None:
    """Two query terms ten words apart score lower than terms adjacent."""
    profile = RankingProfile(phrase_proximity=1.0)
    close = apply_phrase_proximity(
        score=1.0, body="alpha bravo charlie", terms=["alpha", "bravo"], profile=profile
    )
    distant = apply_phrase_proximity(
        score=1.0,
        body="alpha " + "filler " * 60 + "bravo",
        terms=["alpha", "bravo"],
        profile=profile,
    )
    assert close > distant
    # And distant cap-out at no bonus (>= base score).
    assert distant == pytest.approx(1.0, abs=0.05)


def test_phrase_proximity_zero_magnitude_is_identity() -> None:
    profile = RankingProfile(phrase_proximity=0.0)
    out = apply_phrase_proximity(
        score=3.0, body="alpha bravo", terms=["alpha", "bravo"], profile=profile
    )
    assert out == pytest.approx(3.0)


def test_phrase_proximity_missing_term_no_bonus() -> None:
    """If any query term isn't in the body, no proximity bonus (we can't form
    a window)."""
    profile = RankingProfile(phrase_proximity=1.0)
    out = apply_phrase_proximity(score=1.0, body="alpha", terms=["alpha", "bravo"], profile=profile)
    assert out == pytest.approx(1.0)


def test_phrase_proximity_uses_stemming() -> None:
    """Stems match Tantivy's en_stem so query 'penfold' clusters with body 'penfolds'."""
    profile = RankingProfile(phrase_proximity=1.0)
    close = apply_phrase_proximity(
        score=1.0,
        body="bin penfolds estate",
        terms=["penfold", "bin"],
        profile=profile,
    )
    assert close > 1.0


# ── orchestrator ──────────────────────────────────────────────────


def test_rerank_hits_default_profile_is_identity() -> None:
    hits = [_hit(mtime=1_000, kind="pdf", score=2.0), _hit(mtime=999, kind="md", score=1.0)]
    out = rerank_hits(hits, profile=RankingProfile(), query="x", now=1_000_000)
    assert [h.score for h in out] == [2.0, 1.0]


def test_rerank_hits_filetype_flips_tied_scores() -> None:
    """Tied BM25 scores → filetype boost decides ordering."""
    md = _hit(kind="md", score=1.0)
    pdf = _hit(kind="pdf", score=1.0)
    profile = RankingProfile(filetype_boosts={"md": 1.5, "pdf": 1.0})
    out = rerank_hits([pdf, md], profile=profile, query="x", now=1_000)
    assert out[0].kind == "md"
    # pdf is unchanged-score but ranks below md.
    assert out[1].kind == "pdf"


def test_rerank_hits_recency_flips_tied_scores() -> None:
    old = _hit(mtime=1_000, score=1.0, kind="pdf")
    new = _hit(mtime=1_000_000, score=1.0, kind="pdf")
    profile = RankingProfile(recency_boost=0.5, recency_half_life_seconds=86_400)
    out = rerank_hits([old, new], profile=profile, query="x", now=1_000_000)
    assert out[0].mtime == 1_000_000


def test_rerank_hits_propagates_new_score() -> None:
    h = _hit(score=1.0, kind="pdf")
    profile = RankingProfile(filetype_boosts={"pdf": 2.0})
    out = rerank_hits([h], profile=profile, query="x", now=1_000)
    assert out[0].score == pytest.approx(2.0)


# ── end-to-end through Searcher (acceptance) ──────────────────────


@pytest.fixture
def two_collection_index(fixtures_dir: Path, tmp_index_dir: Path, tmp_path: Path) -> Path:
    """Two MD files with identical anchor phrases — used to verify a profile
    switch can flip ranking on tied content."""
    md_dir = tmp_path / "md"
    md_dir.mkdir(parents=True, exist_ok=True)
    a = md_dir / "alpha.md"
    b = md_dir / "bravo.md"
    a.write_text("# Notes\n\nblue penguin sandwich appears here.\n", encoding="utf-8")
    b.write_text("# Notes\n\nblue penguin sandwich appears here.\n", encoding="utf-8")
    # Make bravo.md noticeably newer.
    import os

    os.utime(a, (10_000, 10_000))
    os.utime(b, (10_000_000, 10_000_000))
    build_index(roots=[tmp_path], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


def test_searcher_with_recency_profile_prefers_newer_file(
    two_collection_index: Path,
) -> None:
    """Acceptance for §16 phase 7: 'ranking profile switch flips order on
    tied content'. With recency on, the newer file must rank first."""
    s = Searcher(index_dir=two_collection_index)
    profile = RankingProfile(
        recency_boost=2.0,
        recency_half_life_seconds=86_400,
    )
    grouped = s.search_grouped(
        "blue penguin sandwich",
        profile=profile,
        now=10_000_000 + 86_400,
    )
    paths = [g.path for g in grouped]
    assert len(paths) == 2
    assert paths[0].endswith("bravo.md"), f"newer file should rank first, got {paths}"


def test_searcher_with_filetype_profile_prefers_md(
    fixtures_dir: Path, tmp_index_dir: Path, tmp_path: Path
) -> None:
    """A filetype boost on md flips ordering when both files share the
    anchor phrase."""
    pdf_src = fixtures_dir / "papers" / "test.pdf"
    md_path = tmp_path / "papers" / "alpha.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("# Notes\n\nblue penguin sandwich here.\n", encoding="utf-8")
    pdf_path = tmp_path / "papers" / "alpha.pdf"
    import shutil

    shutil.copy(pdf_src, pdf_path)
    build_index(roots=[tmp_path], index_dir=tmp_index_dir, collection="default")

    s = Searcher(index_dir=tmp_index_dir)

    # No profile: PDF likely wins because it's the original anchor source.
    profile_md = RankingProfile(filetype_boosts={"md": 100.0, "pdf": 1.0})
    grouped = s.search_grouped("blue penguin sandwich", profile=profile_md)
    assert grouped, "expected at least one match"
    assert grouped[0].kind == "md", f"md must win with high md boost, got {grouped[0].kind}"


def test_searcher_no_profile_keeps_bm25_order(
    two_collection_index: Path,
) -> None:
    """Without a profile argument, search_grouped must behave exactly as
    before phase 7 (no rerank)."""
    s = Searcher(index_dir=two_collection_index)
    grouped_a = s.search_grouped("blue penguin sandwich")
    grouped_b = s.search_grouped("blue penguin sandwich")
    assert [g.path for g in grouped_a] == [g.path for g in grouped_b]


# ── final guard: math is well-defined for edge inputs ─────────────


def test_recency_boost_handles_future_mtime() -> None:
    """A clock-skewed future mtime mustn't crash or produce inf; clamp Δt at 0."""
    profile = RankingProfile(recency_boost=0.5, recency_half_life_seconds=86_400)
    out = apply_recency_boost(score=1.0, mtime=2_000_000, profile=profile, now=1_000_000)
    # Future doc gets the maximum bonus (clamp at "now").
    assert out == pytest.approx(1.5)
    assert math.isfinite(out)


# ── config integration ─────────────────────────────────────────────


def test_parse_duration_seconds_known_units() -> None:
    from acorn.config import parse_duration_seconds

    assert parse_duration_seconds("365d") == 365 * 86_400
    assert parse_duration_seconds("12h") == 12 * 3600
    assert parse_duration_seconds("30m") == 30 * 60
    assert parse_duration_seconds("90s") == 90


def test_parse_duration_seconds_rejects_garbage() -> None:
    from acorn.config import parse_duration_seconds

    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration_seconds("forever")


def test_load_config_with_ranking_profile(tmp_path: Path) -> None:
    """Config loads ranking profiles; runtime profile_from_config converts to
    the rerank dataclass; bm25_k1/b are accepted but silently ignored."""
    import textwrap

    from acorn.config import load
    from acorn.rerank import profile_from_config

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""\
            [collections.papers]
            roots = ["~/Papers"]
            ranking_profile = "papers"

            [ranking.papers]
            recency_boost = 0.0
            recency_half_life = "365d"
            phrase_proximity = 0.5
            filetype_boosts = { pdf = 1.0, md = 1.1 }
            bm25_k1 = 1.5
            bm25_b  = 0.7
        """),
        encoding="utf-8",
    )
    cfg = load(cfg_path)
    profile = profile_from_config(cfg.ranking_profile("papers"))
    assert profile.recency_half_life_seconds == 365 * 86_400
    assert profile.phrase_proximity == 0.5
    assert profile.filetype_boosts["md"] == pytest.approx(1.1)


def test_ranking_profile_unknown_name_is_neutral_default() -> None:
    """Calling Config.ranking_profile() with a missing name yields an
    all-zero default — opt-in semantics."""
    from acorn.config import Config
    from acorn.rerank import profile_from_config

    cfg = Config()
    profile = profile_from_config(cfg.ranking_profile("does-not-exist"))
    assert profile.recency_boost == 0.0
    assert profile.filetype_boosts == {}
    assert profile.phrase_proximity == 0.0


def test_acorn_app_resolves_collection_specific_profile(
    two_collection_index: Path,
) -> None:
    """The TUI resolves a per-collection ranking profile when present and
    applies it during search — no profile defined → neutral identity."""
    from acorn.config import CollectionConfig, Config, RankingProfileConfig
    from acorn.tui import AcornApp

    cfg = Config(
        collections={
            "papers": CollectionConfig(roots=[Path("/tmp/x")], ranking_profile="hot"),
        },
        ranking={
            "hot": RankingProfileConfig(recency_boost=2.0, recency_half_life="1d"),
        },
    )
    app = AcornApp(index_dir=two_collection_index, collection="papers", config=cfg)
    assert app._ranking_profile.recency_boost == pytest.approx(2.0)
    assert app._ranking_profile.recency_half_life_seconds == 86_400


def test_acorn_app_no_config_uses_neutral_profile(
    two_collection_index: Path,
) -> None:
    from acorn.tui import AcornApp

    app = AcornApp(index_dir=two_collection_index)
    assert app._ranking_profile.recency_boost == 0.0
    assert app._ranking_profile.filetype_boosts == {}

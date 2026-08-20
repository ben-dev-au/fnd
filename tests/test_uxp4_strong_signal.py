"""UXP-4 §1 — strong-signal regime: bypass fusion when the literal probe
is unambiguous AND no intent supplied."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fnd.config import Config, load
from fnd.index import build_index


def _write_md(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.notes.sources]]
            path = "/tmp/notes"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.fixture
def unambiguous_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """One file owns 'mitochondrion' uniquely; 14 unrelated files exist."""
    a = tmp_path / "notes"
    _write_md(
        a / "biology-cell-organelles.md",
        "# Cell Organelles\n\n## Mitochondrion\nThe mitochondrion is the powerhouse "
        "of the cell. mitochondrion mitochondrion mitochondrion.\n\n"
        "## Other organelles\nNucleus, ribosome, golgi.\n",
    )
    for i in range(14):
        _write_md(a / f"unrelated-{i:02d}.md", f"# Note {i}\n\nFiller content.\n")
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


def test_strong_signal_fires_for_unambiguous_keyword(cfg: Config, unambiguous_index: Path) -> None:
    """`mitochondrion` returns one obvious doc; bypass should fire."""
    from fnd.layered import search_layered
    from fnd.query import Searcher

    searcher = Searcher(index_dir=unambiguous_index)
    groups, trace = search_layered(
        searcher,
        query="mitochondrion",
        limit=10,
        sections_per_file=5,
        collection="notes",
        intent=None,
        with_trace=True,
    )
    assert groups
    assert trace.regime == "strong-signal"
    assert trace.strong_signal.fired is True
    assert trace.fusion is None  # no fusion ran


def test_strong_signal_does_not_fire_for_ambiguous_query(
    cfg: Config, unambiguous_index: Path
) -> None:
    """A common word spread across many docs — bypass must NOT fire;
    fusion runs as default."""
    from fnd.fusion import (
        STRONG_SIGNAL_MIN_NORM_GAP,
        STRONG_SIGNAL_MIN_NORM_SCORE,
        normalise_bm25,
    )
    from fnd.layered import search_layered
    from fnd.query import Searcher

    searcher = Searcher(index_dir=unambiguous_index)
    groups, trace = search_layered(
        searcher,
        query="note filler",
        limit=10,
        sections_per_file=5,
        collection="notes",
        intent=None,
        with_trace=True,
    )
    assert groups  # fusion still finds results
    # Bypass must not fire: regime is fusion (or cascade if extremely sparse).
    assert trace.regime != "strong-signal"
    assert trace.strong_signal.fired is False
    # Probe directly — gap should be tight, normalized top score modest.
    probe = searcher._filtered_raw_hits(
        "note filler", target=100, collection="notes", metadata_filter=None
    )
    assert probe
    if len(probe) >= 2:
        top_n = normalise_bm25(probe[0].score)
        gap = top_n - normalise_bm25(probe[1].score)
        assert not (top_n >= STRONG_SIGNAL_MIN_NORM_SCORE and gap >= STRONG_SIGNAL_MIN_NORM_GAP), (
            "ambiguous query incorrectly classified as strong-signal"
        )


def test_intent_disables_strong_signal_bypass(cfg: Config, unambiguous_index: Path) -> None:
    """Same unambiguous query, but with intent supplied — bypass must
    be disabled, fusion runs."""
    from fnd.layered import _evaluate_strong_signal, search_layered
    from fnd.query import Searcher

    searcher = Searcher(index_dir=unambiguous_index)
    probe = searcher._filtered_raw_hits(
        "mitochondrion", target=100, collection="notes", metadata_filter=None
    )
    ss = _evaluate_strong_signal(probe, intent_present=True)
    assert ss.fired is False
    assert ss.disabled_by_intent is True

    ss_no_intent = _evaluate_strong_signal(probe, intent_present=False)
    assert ss_no_intent.fired is True

    # End-to-end: intent supplied → regime is NOT strong-signal.
    _, trace = search_layered(
        searcher,
        query="mitochondrion",
        limit=10,
        sections_per_file=5,
        collection="notes",
        intent="organelles",
        with_trace=True,
    )
    assert trace.regime != "strong-signal"
    assert trace.strong_signal.disabled_by_intent is True


def test_normalise_bm25_monotone_and_bounded() -> None:
    """Sanity-check the score transform: monotone in [0, 1)."""
    from fnd.fusion import normalise_bm25

    assert normalise_bm25(0.0) == 0.0
    assert normalise_bm25(-5.0) == 0.0  # negative scores clamped to 0
    assert 0.0 < normalise_bm25(0.1) < 1.0
    assert normalise_bm25(1.0) == pytest.approx(0.5)
    # Monotone — higher input → higher output
    assert normalise_bm25(0.5) < normalise_bm25(1.5) < normalise_bm25(5.0)
    # Asymptote — large values approach but never reach 1
    assert 0.99 < normalise_bm25(100.0) < 1.0

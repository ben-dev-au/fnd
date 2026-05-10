"""UXP-4 §2 — explain trace shape and content."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from acorn.config import Config, load
from acorn.index import build_index


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
    monkeypatch.setattr("acorn.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.fixture
def medium_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """8 docs that all mention "quick brown fox" — fusion's the right
    regime here (lots of candidates, no clear winner)."""
    a = tmp_path / "notes"
    for i in range(8):
        _write_md(
            a / f"doc-{i:02d}.md",
            f"# Doc {i}\n\n## Section\nThe quick brown fox jumps over file {i}. "
            f"More content about the quick brown fox in document {i}.\n",
        )
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


@pytest.fixture
def unambiguous_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    a = tmp_path / "notes"
    _write_md(
        a / "biology-cell-organelles.md",
        "# Cell Organelles\n\n## Mitochondrion\nThe mitochondrion is the powerhouse "
        "of the cell. mitochondrion mitochondrion mitochondrion.\n",
    )
    for i in range(14):
        _write_md(a / f"unrelated-{i:02d}.md", f"# Note {i}\n\nFiller content.\n")
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


def test_fusion_trace_shape(cfg: Config, medium_index: Path) -> None:
    """A multi-word query routes through fusion; trace must list one
    SubQueryTrace per sub-query that ran (phrase + lex), with hit_count
    and bm25_top populated."""
    from acorn.fusion import fusion_search
    from acorn.query import Searcher

    searcher = Searcher(index_dir=medium_index)
    hits, trace = fusion_search(
        searcher,
        query="quick brown",
        limit=10,
        collection="notes",
        with_trace=True,
    )
    assert hits
    sources = {s.source for s in trace.subqueries}
    assert "phrase" in sources
    assert "lex" in sources
    for s in trace.subqueries:
        assert s.hit_count >= 0
        if s.hit_count > 0:
            assert s.bm25_top > 0.0
        assert s.rrf_k == 60
    # Per-hit contribution covers each returned hit.
    keys = {(c.parent_id, c.chunk_seq) for c in trace.contributions}
    for h in hits:
        assert (h.parent_id, h.chunk_seq) in keys
    # Final score on the trace mirrors the BM25 score on the Hit.
    by_key = {(c.parent_id, c.chunk_seq): c for c in trace.contributions}
    top_h = hits[0]
    top_c = by_key[(top_h.parent_id, top_h.chunk_seq)]
    assert top_c.final_score == pytest.approx(top_h.score)


def test_fusion_search_default_returns_list_unchanged(cfg: Config, medium_index: Path) -> None:
    """Backward-compat: no with_trace kwarg → returns plain list[Hit]."""
    from acorn.fusion import fusion_search
    from acorn.query import Searcher

    searcher = Searcher(index_dir=medium_index)
    hits = fusion_search(searcher, query="quick brown", limit=10, collection="notes")
    assert isinstance(hits, list)
    assert hits


def test_cascade_trace_records_pass_widening(
    cfg: Config, tmp_path: Path, tmp_index_dir: Path
) -> None:
    """A typo'd single-word query forces fuzzy-pass widening; cascade
    trace lists the literal pass with zero new_count and the fuzzy pass
    with hits.

    Uses ``glimmer`` / ``glimer`` (both pass through en_stem unchanged
    so the on-disk Levenshtein distance is exactly 1) — same canonical
    fixture as ``test_ux_j_cascade_fallback.py``."""
    from acorn.cascade import cascade_search
    from acorn.query import Searcher

    a = tmp_path / "notes"
    a.mkdir(parents=True, exist_ok=True)
    (a / "doc.md").write_text("# Doc\n\nthe glimmer pattern is shown here.\n", encoding="utf-8")
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")

    searcher = Searcher(index_dir=tmp_index_dir)
    hits, trace = cascade_search(
        searcher,
        query="glimer",  # 1-edit typo of glimmer
        threshold=10,
        limit=10,
        collection="notes",
        with_trace=True,
    )
    pass_names = [p.name for p in trace.passes]
    assert "literal" in pass_names
    assert "fuzzy" in pass_names
    literal_pass = next(p for p in trace.passes if p.name == "literal")
    fuzzy_pass = next(p for p in trace.passes if p.name == "fuzzy")
    assert literal_pass.new_count == 0  # typo doesn't match literal
    assert fuzzy_pass.new_count > 0  # fuzzy~1 finds 'glimmer'
    assert hits  # fuzzy pass surfaced the hit


def test_search_layered_regime_strong_signal(cfg: Config, unambiguous_index: Path) -> None:
    from acorn.layered import search_layered
    from acorn.query import Searcher

    searcher = Searcher(index_dir=unambiguous_index)
    _, trace = search_layered(
        searcher,
        query="mitochondrion",
        limit=10,
        sections_per_file=5,
        collection="notes",
        with_trace=True,
    )
    assert trace.regime == "strong-signal"
    assert trace.fusion is None
    assert trace.cascade is None


def test_search_layered_regime_fusion_for_ambiguous_query(cfg: Config, medium_index: Path) -> None:
    from acorn.layered import search_layered
    from acorn.query import Searcher

    searcher = Searcher(index_dir=medium_index)
    _, trace = search_layered(
        searcher,
        query="brown fox",
        limit=10,
        sections_per_file=5,
        collection="notes",
        with_trace=True,
    )
    assert trace.regime == "fusion"
    assert trace.fusion is not None
    assert trace.cascade is None


def test_search_layered_regime_cascade_for_typo(
    cfg: Config, tmp_path: Path, tmp_index_dir: Path
) -> None:
    from acorn.layered import search_layered
    from acorn.query import Searcher

    a = tmp_path / "notes"
    a.mkdir(parents=True, exist_ok=True)
    (a / "doc.md").write_text("# Doc\n\nthe glimmer pattern is shown here.\n", encoding="utf-8")
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")

    searcher = Searcher(index_dir=tmp_index_dir)
    _, trace = search_layered(
        searcher,
        query="glimer",  # 1-edit typo; literal probe returns 0 hits
        limit=10,
        sections_per_file=5,
        collection="notes",
        with_trace=True,
    )
    assert trace.regime.startswith("cascade")
    assert trace.cascade is not None


def test_trace_to_json_is_valid_dict(cfg: Config, medium_index: Path) -> None:
    """SearchTrace.to_json round-trips through json module without error."""
    import json

    from acorn.layered import search_layered
    from acorn.query import Searcher

    searcher = Searcher(index_dir=medium_index)
    _, trace = search_layered(
        searcher,
        query="brown fox",
        limit=5,
        sections_per_file=3,
        collection="notes",
        with_trace=True,
    )
    payload = trace.to_json()
    # json.dumps must succeed (no non-serializable types lurking).
    serialized = json.dumps(payload)
    reparsed = json.loads(serialized)
    assert reparsed["query"] == "brown fox"
    assert reparsed["regime"] == "fusion"
    assert "fusion" in reparsed
    assert "subqueries" in reparsed["fusion"]

"""UX-J regression: fusion-as-default must not collapse result count
or compress BM25 scores into the RRF range.

The previous wiring called ``fusion_search`` with ``limit=50`` which
made each sub-query pull only 50 hits and capped the output at 50
chunks; ``group_by_file`` then bucketed those 50 chunks into 2-3
files. Result: a corpus where the old single-pass surfaced 27 files
collapsed to 2.

Score-wise, RRF arithmetic produced ``score ≈ 0.07`` for rank-1 hits,
visible to the user as a misleading "low confidence" signal even when
exact-match BM25 scores were 30+.

These tests pin both behaviours.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from acorn.config import Config, load
from acorn.index import build_index
from acorn.tui import AcornApp


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
def wide_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """Build a corpus where 'templates' appears across many files so the
    grouper has plenty of candidates. Each file has multiple sections so
    the chunk pool is reasonably wide too."""
    a = tmp_path / "notes"
    for i in range(15):
        body = "\n".join(
            [
                f"# File {i}",
                "",
                "## Section A",
                f"templates are central to chapter {i}.",
                "",
                "## Section B",
                f"more on templates and their role in {i}.",
                "",
                "## Section C",
                "unrelated filler content here.",
            ]
        )
        _write_md(a / f"notes-{i:02d}.md", body)
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_fusion_default_returns_many_files(cfg: Config, wide_index: Path) -> None:
    """A corpus with templates in 15 files should produce 15 result rows
    — the fusion path must oversample enough chunks for the grouper to
    fill ``limit`` files. The pre-fix wiring produced 2-3 files."""
    app = AcornApp(index_dir=wide_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        app._run_query("templates")
        await pilot.pause()
        assert len(app._groups) >= 10, (
            f"expected >=10 files for a 15-file 'templates' corpus, " f"got {len(app._groups)}"
        )


@pytest.mark.asyncio
async def test_fusion_preserves_bm25_score_range(cfg: Config, wide_index: Path) -> None:
    """Hit scores must stay in the BM25 range (1+) — RRF fused values
    of 0.001-0.07 leak the implementation through to the UI."""
    app = AcornApp(index_dir=wide_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        app._run_query("templates")
        await pilot.pause()
        assert app._groups
        top = app._groups[0].hits[0]
        # RRF-fused scores cap at ~0.13 even when a doc ranks #1 in
        # every sub-query (phrase + lex + syn at weights 2.0/1.0/0.6,
        # k=60, plus the 0.05 rank-1 bonus). BM25 for our fixture sits
        # well above that. Pinning at >=0.2 catches the regression
        # without being brittle to the indexer's exact BM25 output.
        assert top.score >= 0.2, (
            f"top hit score {top.score} looks like RRF arithmetic "
            "rather than BM25; fusion must preserve the BM25 score "
            "and only use RRF for ordering."
        )

        # Belt-and-braces: the score on the hit must equal the BM25
        # score the searcher returns for the same doc through the
        # single-pass path. RRF would diverge.
        searcher = app._searcher
        assert searcher is not None
        raw = searcher._filtered_raw_hits(
            "templates", target=500, collection="notes", metadata_filter=None
        )
        bm25 = {(h.parent_id, h.chunk_seq): h.score for h in raw}
        key = (top.parent_id, top.chunk_seq)
        assert key in bm25, "top hit absent from single-pass results"
        # The reranker may scale scores slightly, but they should be
        # within an order of magnitude — RRF would be off by 10x.
        assert top.score >= bm25[key] * 0.5, (
            f"top hit score {top.score} diverges from single-pass "
            f"BM25 {bm25[key]}; fusion shouldn't rewrite scores."
        )

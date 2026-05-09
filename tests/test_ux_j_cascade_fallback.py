"""UX-J: cascade auto-fallback wired into the TUI default search path.

When fusion's combined output is sparse, ``_run_query`` should widen
via ``cascade_search`` so a single-typo query (caught by the cascade's
fuzzy~1 pass) still surfaces the right document.
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
def fuzzy_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """Index a doc whose body uses ``glimmer`` so a 1-edit typo
    (``glimer``) is the canonical fuzzy fallback case. Cascade's
    ``_fuzzy_pass`` uses ``fuzzy_term_query`` against raw indexed
    tokens — both ``glimmer`` and ``glimer`` pass through en_stem
    unchanged, so the on-disk Levenshtein distance is exactly 1."""
    a = tmp_path / "notes"
    _write_md(a / "notes.md", "# Notes\nthe glimmer pattern is shown here.\n")
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_typo_query_falls_back_to_cascade(cfg: Config, fuzzy_index: Path) -> None:
    """``glimer`` (1-edit typo) exact-misses but cascade's fuzzy~1 pass
    surfaces ``glimmer``. Run via ``_run_query``; expect a group."""
    app = AcornApp(index_dir=fuzzy_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        app._run_query("glimer")
        await pilot.pause()
        assert app._groups, (
            "fusion+cascade should surface notes.md via fuzzy~1; " f"got {app._groups!r}"
        )
        assert app._groups[0].path.endswith("notes.md")
        # Cascade tags fuzzy hits with pass_index == 1; TUI renders the
        # ``~`` glyph for those.
        assert any(h.pass_index == 1 for g in app._groups for h in g.hits)


@pytest.mark.asyncio
async def test_exact_query_uses_fusion_path(cfg: Config, fuzzy_index: Path) -> None:
    """A clean exact-match query (no typos) should be served by the
    fusion path — pass_index == 0 (lex) is the default attribution."""
    app = AcornApp(index_dir=fuzzy_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        app._run_query("glimmer")
        await pilot.pause()
        assert app._groups
        top = app._groups[0].hits[0]
        assert top.pass_index == 0


@pytest.mark.asyncio
async def test_cascade_path_honours_active_sources(cfg: Config, fuzzy_index: Path) -> None:
    """When ``active_sources`` is set, every cascade pass (including the
    programmatic fuzzy pass) should respect the source-set so a bogus
    path filters out hits even via fuzzy."""
    from acorn.cascade import cascade_search
    from acorn.query import Searcher

    s = Searcher(index_dir=fuzzy_index)
    out = cascade_search(
        s,
        query="glimer",
        threshold=10,
        limit=10,
        collection="notes",
        active_sources=["/no/such/source"],
    )
    assert out == [], out

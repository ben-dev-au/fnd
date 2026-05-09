"""UX-H: Fuzzy filter — auto-fuzz body terms via tantivy ``fuzzy_fields``."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Tree

from acorn.config import Config, load
from acorn.index import build_index
from acorn.query import Searcher
from acorn.state import UiState
from acorn.state import load as load_state
from acorn.state import save as save_state
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
    """Index a small md corpus with words whose stem differs from the
    typo'd query — required because the body field is ``en_stem``, so
    fuzzy distance is measured between query-stem and stored-stem.
    ``decuple`` and ``decouple`` stem differently, so fuzzy_distance=1
    is needed to bridge the gap.
    """
    a = tmp_path / "notes"
    _write_md(a / "iterators.md", "# Notes\nIterators decouple traversal nicely.\n")
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


def test_searcher_fuzzy_distance_1_catches_typos(fuzzy_index: Path) -> None:
    """``fuzzy_distance=1`` catches a single-typo query (``decuple``,
    a 1-edit miss for ``decouple``) that exact search misses."""
    s = Searcher(index_dir=fuzzy_index)
    assert s.search("decuple", collection="notes") == []
    fuzzy_hits = s.search("decuple", collection="notes", fuzzy_distance=1)
    assert len(fuzzy_hits) == 1
    assert fuzzy_hits[0].path.endswith("iterators.md")


def test_searcher_fuzzy_distance_2_catches_heavier_typos(fuzzy_index: Path) -> None:
    s = Searcher(index_dir=fuzzy_index)
    # ``iterater`` differs further from ``iterators`` after stemming;
    # fuzzy distance 2 is needed to bridge the gap.
    assert s.search("iterater", collection="notes", fuzzy_distance=1) == []
    out = s.search("iterater", collection="notes", fuzzy_distance=2)
    assert len(out) == 1


def test_state_persists_fuzzy_filter(tmp_path: Path) -> None:
    p = tmp_path / "scope.toml"
    save_state(UiState(filter_fuzzy=2), p)
    assert load_state(p).filter_fuzzy == 2


def test_state_clamps_fuzzy_to_valid_range(tmp_path: Path) -> None:
    """A bogus on-disk value (negative, too large) clamps to [0, 2]."""
    p = tmp_path / "scope.toml"
    p.write_text(
        "[scope]\ncollections = []\nsources = []\n[filters]\nfuzzy = 99\n",
        encoding="utf-8",
    )
    assert load_state(p).filter_fuzzy == 2


@pytest.mark.asyncio
async def test_filters_panel_shows_fuzzy_branch(cfg: Config, fuzzy_index: Path) -> None:
    app = AcornApp(index_dir=fuzzy_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        labels = "\n".join(str(c.label) for c in tree.root.children)
        assert "Fuzzy" in labels, labels


@pytest.mark.asyncio
async def test_panel_toggle_drives_fuzzy_search(cfg: Config, fuzzy_index: Path) -> None:
    """Setting fuzzy = 1 in the panel makes a typo query find the right file."""
    app = AcornApp(index_dir=fuzzy_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        # Exact: no results for the typo.
        app._run_query("decuple")
        await pilot.pause()
        assert app._groups == []
        # Set fuzzy=1, re-run: we get the fuzzy hit.
        app._filter_fuzzy = 1
        app._run_query("decuple")
        await pilot.pause()
        assert app._groups, "fuzzy=1 should match the iterators.md file"


@pytest.mark.asyncio
async def test_panel_toggle_persists_fuzzy(cfg: Config, fuzzy_index: Path) -> None:
    app = AcornApp(index_dir=fuzzy_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._filter_fuzzy = 2
        app._persist_state()
        await pilot.pause()
    app2 = AcornApp(index_dir=fuzzy_index, config=cfg)
    assert app2._filter_fuzzy == 2

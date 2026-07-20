"""The pinned Clear-filters bar below the filters tree (never scrolls)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Static, Tree

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.papers.sources]]
            path = "/tmp/papers"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.fixture
def idx(tmp_path: Path, tmp_index_dir: Path) -> Path:
    root = tmp_path / "papers"
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.md").write_text("---\ntags: [recipe]\n---\n\n# A\n\nsaffron\n", encoding="utf-8")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="papers")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_bar_hidden_when_no_filters(cfg: Config, idx: Path) -> None:
    app = FNDApp(index_dir=idx, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.query_one("#clear_filters_bar", Static)
        assert bar.display is False


@pytest.mark.asyncio
async def test_bar_appears_when_a_filter_is_active(cfg: Config, idx: Path) -> None:
    app = FNDApp(index_dir=idx, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._scope.filter_kinds = ["md"]
        app._scope.refresh_filters_panel()
        await pilot.pause()
        bar = app.query_one("#clear_filters_bar", Static)
        assert bar.display is True
        assert "Clear all filters" in str(bar.render())


@pytest.mark.asyncio
async def test_bar_hides_again_when_cleared(cfg: Config, idx: Path) -> None:
    app = FNDApp(index_dir=idx, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._scope.filter_kinds = ["md"]
        app._scope.refresh_filters_panel()
        await pilot.pause()
        app._scope.clear_filters()
        await pilot.pause()
        assert app.query_one("#clear_filters_bar", Static).display is False


@pytest.mark.asyncio
async def test_clicking_the_bar_clears(cfg: Config, idx: Path) -> None:
    app = FNDApp(index_dir=idx, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._scope.filter_kinds = ["md"]
        app._scope.tag_include = {"frontmatter": {"recipe"}}
        app._scope.refresh_filters_panel()
        await pilot.pause()
        await pilot.click("#clear_filters_bar")
        await pilot.pause()
        assert app._scope.has_active_filters is False
        assert app.query_one("#clear_filters_bar", Static).display is False


@pytest.mark.asyncio
async def test_no_clear_row_inside_the_tree(cfg: Config, idx: Path) -> None:
    """The affordance is the pinned bar now, not an in-tree row that could be
    inserted above the viewport."""
    app = FNDApp(index_dir=idx, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._scope.filter_kinds = ["md"]
        app._scope.refresh_filters_panel()
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        assert not any("Clear all filters" in str(n.label) for n in tree.root.children)

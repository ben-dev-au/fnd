"""Clear-all-filters: controller reset, pane row, and the bound action."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Tree
from textual.widgets.tree import TreeNode

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


def _dirty(app: FNDApp) -> None:
    sc = app._scope
    sc.filter_kinds = ["md"]
    sc.filter_date = "week"
    sc.filter_created = "year"
    sc.tag_include = {"frontmatter": {"recipe"}}
    sc.tag_exclude = {"frontmatter": {"draft"}}
    sc.tag_match_all = False


def test_has_active_filters_reflects_state(cfg: Config, idx: Path) -> None:
    import asyncio

    async def go() -> None:
        app = FNDApp(index_dir=idx, config=cfg)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._scope.has_active_filters is False
            app._scope.filter_kinds = ["md"]
            assert app._scope.has_active_filters is True

    asyncio.run(go())


@pytest.mark.asyncio
async def test_clear_filters_resets_every_filter(cfg: Config, idx: Path) -> None:
    app = FNDApp(index_dir=idx, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _dirty(app)
        app._scope.clear_filters()
        await pilot.pause()
        sc = app._scope
        assert sc.filter_kinds == []
        assert sc.filter_date == "any"
        assert sc.filter_created == "any"
        assert sc.tag_include == {}
        assert sc.tag_exclude == {}
        assert sc.tag_match_all is True
        assert sc.has_active_filters is False


@pytest.mark.asyncio
async def test_clear_filters_leaves_scope_untouched(cfg: Config, idx: Path) -> None:
    """Collections/sources are scope, not filters — clear must not touch them."""
    app = FNDApp(index_dir=idx, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        before = dict(app._scope.selection)
        _dirty(app)
        app._scope.clear_filters()
        await pilot.pause()
        assert dict(app._scope.selection) == before


@pytest.mark.asyncio
async def test_clear_row_appears_only_when_active(cfg: Config, idx: Path) -> None:
    app = FNDApp(index_dir=idx, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)

        def clear_row() -> TreeNode[Any] | None:
            for n in tree.root.children:
                if "Clear" in str(n.label):
                    return n
            return None

        assert clear_row() is None
        app._scope.filter_kinds = ["md"]
        app._scope.refresh_filters_panel()
        await pilot.pause()
        assert clear_row() is not None


@pytest.mark.asyncio
async def test_clear_row_selection_clears(cfg: Config, idx: Path) -> None:
    app = FNDApp(index_dir=idx, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _dirty(app)
        app._scope.refresh_filters_panel()
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        row = next(n for n in tree.root.children if "Clear" in str(n.label))
        tree.select_node(row)
        await pilot.pause()
        assert app._scope.has_active_filters is False


@pytest.mark.asyncio
async def test_clear_persists(cfg: Config, idx: Path) -> None:
    app = FNDApp(index_dir=idx, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _dirty(app)
        app._scope.clear_filters()
        await pilot.pause()

    app2 = FNDApp(index_dir=idx, config=cfg)
    async with app2.run_test() as pilot2:
        await pilot2.pause()
        assert app2._scope.has_active_filters is False


@pytest.mark.asyncio
async def test_clear_filters_action_exists_and_clears(cfg: Config, idx: Path) -> None:
    app = FNDApp(index_dir=idx, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _dirty(app)
        app.action_clear_filters()
        await pilot.pause()
        assert app._scope.has_active_filters is False


def test_clear_filters_action_is_registered() -> None:
    from fnd.tui.actions import REGISTRY

    ids = {a.id for a in REGISTRY}
    assert "clear_filters" in ids

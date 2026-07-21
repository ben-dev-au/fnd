"""Created filter: radio behaviour, persistence, query composition.

Mirrors tests/test_ux_f_filters_panel.py, including its low-level spy:
fusion issues several sub-queries and only one need carry the filter.
"""

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
def cfg_one_collection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
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
def mixed_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    a = tmp_path / "papers"
    a.mkdir(parents=True, exist_ok=True)
    (a / "a.md").write_text("# A\nshared anchor: glimmer\n", encoding="utf-8")
    (a / "b.txt").write_text("shared anchor: glimmer\n", encoding="utf-8")
    build_index(roots=[a], index_dir=tmp_index_dir, collection="papers")
    return tmp_index_dir


def _branch(tree: Tree[Any], label: str) -> TreeNode[Any]:
    for node in tree.root.children:
        if label in str(node.label):
            return node
    raise AssertionError(f"branch {label!r} not found")


def _leaf(branch: TreeNode[Any], value: str) -> TreeNode[Any]:
    # Leading space avoids matching the ●/○ marker.
    return next(c for c in branch.children if f" {value}" in str(c.label))


@pytest.mark.asyncio
async def test_created_branch_exists(cfg_one_collection: Config, mixed_index: Path) -> None:
    app = FNDApp(index_dir=mixed_index, config=cfg_one_collection)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert _branch(app.query_one("#filters_panel_tree", Tree), "Created") is not None


@pytest.mark.asyncio
async def test_created_is_single_select(cfg_one_collection: Config, mixed_index: Path) -> None:
    app = FNDApp(index_dir=mixed_index, config=cfg_one_collection)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        branch = _branch(tree, "Created")
        branch.expand()
        await pilot.pause()
        tree.select_node(_leaf(branch, "week"))
        await pilot.pause()
        assert app._scope.filter_created == "week"

        branch = _branch(tree, "Created")
        tree.select_node(_leaf(branch, "month"))
        await pilot.pause()
        # Replaced, not accumulated.
        assert app._scope.filter_created == "month"


@pytest.mark.asyncio
async def test_created_and_modified_are_independent(
    cfg_one_collection: Config, mixed_index: Path
) -> None:
    app = FNDApp(index_dir=mixed_index, config=cfg_one_collection)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        created = _branch(tree, "Created")
        created.expand()
        await pilot.pause()
        tree.select_node(_leaf(_branch(tree, "Created"), "week"))
        await pilot.pause()

        modified = _branch(tree, "Modified")
        modified.expand()
        await pilot.pause()
        tree.select_node(_leaf(_branch(tree, "Modified"), "year"))
        await pilot.pause()

        assert app._scope.filter_created == "week"
        assert app._scope.filter_date == "year"


@pytest.mark.asyncio
async def test_created_composes_into_the_query(
    cfg_one_collection: Config, mixed_index: Path
) -> None:
    app = FNDApp(index_dir=mixed_index, config=cfg_one_collection)
    async with app.run_test() as pilot:
        await pilot.pause()
        seen: list[str] = []
        searcher = app._search.searcher
        assert searcher is not None
        original = searcher._filtered_raw_hits

        def spy(query: str, **kwargs: object) -> list[object]:
            seen.append(query)
            return original(query, **kwargs)  # type: ignore[no-any-return,arg-type]

        searcher._filtered_raw_hits = spy  # type: ignore[method-assign]
        app._scope.filter_created = "week"
        app._search.run("glimmer")
        await pilot.pause()
        assert any("created:" in q for q in seen), seen


@pytest.mark.asyncio
async def test_created_persists_across_restart(
    cfg_one_collection: Config, mixed_index: Path
) -> None:
    app = FNDApp(index_dir=mixed_index, config=cfg_one_collection)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        branch = _branch(tree, "Created")
        branch.expand()
        await pilot.pause()
        tree.select_node(_leaf(branch, "month"))
        await pilot.pause()

    app2 = FNDApp(index_dir=mixed_index, config=cfg_one_collection)
    async with app2.run_test() as pilot2:
        await pilot2.pause()
        assert app2._scope.filter_created == "month"


@pytest.mark.asyncio
async def test_created_enter_toggles_off(cfg_one_collection: Config, mixed_index: Path) -> None:
    """Enter on the selected value clears it back to 'any' — no separate 'any'
    row to navigate to, matching every other filter toggle."""
    app = FNDApp(index_dir=mixed_index, config=cfg_one_collection)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        branch = _branch(tree, "Created")
        branch.expand()
        await pilot.pause()
        assert not any(" any" in str(c.label) for c in branch.children), "no 'any' row"

        tree.select_node(next(c for c in branch.children if " week" in str(c.label)))
        await pilot.pause()
        assert app._scope.filter_created == "week"

        # Second Enter on the same value toggles it off.
        branch = _branch(tree, "Created")
        tree.select_node(next(c for c in branch.children if " week" in str(c.label)))
        await pilot.pause()
        assert app._scope.filter_created == "any"

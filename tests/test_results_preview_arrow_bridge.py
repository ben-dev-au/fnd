"""Arrow-bridge between the results tree and the preview pane.

Smooth keyboard flow: cursor on a hit (leaf) in ``#results_pane`` →
Right hands focus to ``#preview_pane``; from there Left hands focus
back to the results tree. No mouse, no Tab, no leaving the arrow keys.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Tree

from fnd.index import build_index
from fnd.tui import FNDApp


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_right_on_results_leaf_focuses_preview(built_index: Path) -> None:
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        assert tree.root.children
        top_file = tree.root.children[0]
        assert top_file.is_expanded
        assert top_file.children
        # The default cursor placement after a query lands on the first
        # hit child — a leaf.
        assert tree.cursor_node is top_file.children[0]
        await pilot.press("right")
        await pilot.pause()
        preview = app.query_one("#preview_pane")
        assert preview.has_focus, "Right on a results leaf should focus the preview"


@pytest.mark.asyncio
async def test_left_on_preview_returns_to_results(built_index: Path) -> None:
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        preview = app.query_one("#preview_pane")
        preview.focus()
        await pilot.pause()
        assert preview.has_focus
        await pilot.press("left")
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        assert tree.has_focus, "Left on the preview pane should focus the results tree"


@pytest.mark.asyncio
async def test_right_on_collapsed_file_still_expands(built_index: Path) -> None:
    """Right on a collapsed file row keeps its original behaviour:
    expand the row and move the cursor onto its first child. The
    arrow-bridge only kicks in on actual leaves, so file navigation
    isn't shortcut into the preview pane."""
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        assert tree.root.children
        top_file = tree.root.children[0]
        top_file.collapse()
        await pilot.pause()
        tree.focus()
        tree.move_cursor(top_file)
        await pilot.pause()
        await pilot.press("right")
        await pilot.pause()
        assert top_file.is_expanded
        assert tree.cursor_node is top_file.children[0]
        preview = app.query_one("#preview_pane")
        assert not preview.has_focus

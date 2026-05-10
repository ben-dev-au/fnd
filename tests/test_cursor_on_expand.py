"""Cursor placement on results-row expansion.

When the TUI auto-expands the top result on a new query (or the user
presses Right on a collapsed file row), the cursor must drop onto the
first hit *child* of that file. Leaving it on the parent file row
forces a redundant Down keypress before navigation actually advances
to a fresh match — the preview is already showing the file's first
hit, so re-selecting it via the parent contributes nothing.

Both moves rely on a ``call_after_refresh`` defer because Textual's
Tree only assigns line indices to newly-mounted children on the next
render tick — calling ``move_cursor`` synchronously after
``node.expand()`` (or after a fresh ``tree.clear()`` + adds) silently
misses the line and the cursor stays on the parent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Tree

from acorn.index import build_index
from acorn.tui import AcornApp


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_initial_query_lands_cursor_on_first_hit(built_index: Path) -> None:
    """``--query`` populates results AND seats the cursor on the first
    hit of the auto-expanded top file."""
    app = AcornApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        assert tree.root.children, "expected at least one result"
        top_file = tree.root.children[0]
        assert top_file.is_expanded
        assert top_file.children, "top file should have hit children"
        assert tree.cursor_node is top_file.children[0]


@pytest.mark.asyncio
async def test_right_arrow_on_collapsed_file_drops_cursor_on_first_child(
    built_index: Path,
) -> None:
    """Right on a collapsed file → expand it AND move the cursor to its
    first hit (so the next Down advances to the second hit, not the
    first)."""
    app = AcornApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        assert tree.root.children
        first_file = tree.root.children[0]
        first_file.collapse()
        await pilot.pause()
        tree.focus()
        tree.move_cursor(first_file)
        await pilot.pause()
        assert tree.cursor_node is first_file
        await pilot.press("right")
        await pilot.pause()
        assert first_file.is_expanded
        assert first_file.children
        assert tree.cursor_node is first_file.children[0]

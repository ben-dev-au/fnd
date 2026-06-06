"""Cursor placement on results-row expansion.

When the TUI auto-expands the top result on a new query (or the user
presses Right on a collapsed file row), the cursor must drop onto the
first hit *child* of that file. Leaving it on the parent file row
forces a redundant Down keypress before navigation actually advances
to a fresh match — the preview is already showing the file's first
hit, so re-selecting it via the parent contributes nothing.

``node.expand()`` only invalidates the tree's line cache; the new
children keep a stale ``_line`` of -1 until a rebuild. Moving via
``move_cursor(child)`` then reads -1 and the skip-expanded-parents
validator parks the cursor on the first file — visible when the rebuild
lags (preview pane mid full-mount). The move goes through
``move_cursor_to_line(node.line + 1)``, which forces the rebuild.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.app import ResultsTree


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


class _ResultsTreeApp(App[None]):
    """Bare host for a ``ResultsTree`` so the stale-line race can be
    reproduced deterministically with several file rows — the real
    corpus yields too few groups to tell a wrong "first file" jump apart
    from a correct "first hit" landing."""

    def compose(self) -> ComposeResult:
        tree: ResultsTree = ResultsTree("root")
        tree.show_root = False
        tree._skip_expanded_parents = True  # type: ignore[attr-defined]
        tree.id = "t"
        yield tree


def _seed_files(tree: ResultsTree, n_files: int, n_hits: int) -> list[TreeNode[dict[str, Any]]]:
    files: list[TreeNode[dict[str, Any]]] = []
    for f in range(n_files):
        node = tree.root.add(f"file {f}", data={"kind": "file"}, expand=False)
        for h in range(n_hits):
            node.add_leaf(f"hit {f}.{h}", data={"kind": "section"})
        files.append(node)
    return files


@pytest.mark.asyncio
async def test_naive_move_cursor_with_stale_line_jumps_to_first_file() -> None:
    """Characterizes the root cause: ``move_cursor(child)`` while the
    child's line index is stale (-1) sets ``cursor_line`` to -1, which the
    skip-expanded-parents validator clamps to 0 and parks on the first
    file — the bug the production helper must avoid."""
    app = _ResultsTreeApp()
    async with app.run_test() as pilot:
        tree = app.query_one("#t", ResultsTree)
        files = _seed_files(tree, n_files=4, n_hits=3)
        await pilot.pause()
        target = files[2]
        tree.focus()
        tree.move_cursor(target)
        await pilot.pause()

        # The state the instant after expand(), before the tree rebuilds:
        # children carry a stale _line of -1 and the cache is invalidated.
        target.expand()
        for ch in target.children:
            ch._line = -1
        tree._invalidate()

        tree.move_cursor(target.children[0])
        assert tree.cursor_node is files[0]  # jumped to the first file


@pytest.mark.asyncio
async def test_first_child_move_is_robust_to_stale_line_cache() -> None:
    """The production helper lands the cursor on the expanded file's first
    hit even when the line cache is stale at call time (the race that
    surfaces while the preview pane is mid full-mount)."""
    app = _ResultsTreeApp()
    async with app.run_test() as pilot:
        tree = app.query_one("#t", ResultsTree)
        files = _seed_files(tree, n_files=4, n_hits=3)
        await pilot.pause()
        target = files[2]
        tree.focus()
        tree.move_cursor(target)
        await pilot.pause()

        target.expand()
        for ch in target.children:
            ch._line = -1
        tree._invalidate()

        FNDApp._move_cursor_to_first_child(tree, target)
        assert tree.cursor_node is target.children[0]


@pytest.mark.asyncio
async def test_initial_query_lands_cursor_on_first_hit(built_index: Path) -> None:
    """``--query`` populates results AND seats the cursor on the first
    hit of the auto-expanded top file."""
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
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
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
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

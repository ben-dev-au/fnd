"""UX-B: Left arrow collapses parent from child (lazygit semantics).

The standard Textual Tree binding for ``left`` only collapses the
focused node — useless when the cursor is on a leaf, since there's
nothing under the leaf to collapse. Lazygit's UX: ``left`` on a leaf
moves the cursor to its parent and collapses *that*. ``left`` on an
already-collapsed branch behaves the same — keep walking up. This
turns ``left`` into a single-key "back out" gesture.
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
async def test_left_on_leaf_collapses_parent(built_index: Path) -> None:
    """Cursor on a section (leaf) → Left collapses the parent file node
    and moves the cursor onto it."""
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        first_file = next(iter(tree.root.children))
        first_file.expand()
        await pilot.pause()
        tree.focus()
        await pilot.press("down")  # cursor onto first section (leaf)
        await pilot.pause()
        focused = tree.cursor_node
        assert focused is not None
        # Sanity: cursor is on a leaf whose parent is the file node.
        assert focused.parent is first_file
        # Press Left — should collapse first_file and move cursor onto it.
        await pilot.press("left")
        await pilot.pause()
        assert not first_file.is_expanded, "Left on a leaf should collapse its parent"
        assert (
            tree.cursor_node is first_file
        ), "Left on a leaf should move the cursor up to the parent"


@pytest.mark.asyncio
async def test_left_on_collapsed_branch_walks_further_up(built_index: Path) -> None:
    """Cursor on an already-collapsed file node at the top level → Left
    is a no-op (no parent to collapse). It must not crash, must not move
    the cursor off the tree."""
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        # The top result auto-expands; collapse it so the cursor below
        # lands on a top-level *collapsed* node (the scenario under test).
        first = next(iter(tree.root.children))
        first.collapse()
        await pilot.pause()
        tree.focus()
        await pilot.press("down")
        await pilot.pause()
        focused = tree.cursor_node
        assert focused is not None
        assert not focused.is_expanded
        before = tree.cursor_node
        await pilot.press("left")
        await pilot.pause()
        # No crash, cursor stayed where it was.
        assert tree.cursor_node is before


@pytest.mark.asyncio
async def test_left_on_expanded_branch_just_collapses_it(built_index: Path) -> None:
    """Cursor on an expanded branch's first child → Left collapses the
    parent in place, then the cursor falls back to the (now collapsed)
    parent row because the section row is no longer visible.

    Note: the results tree treats *expanded* file rows as unselectable
    (``ResultsTree.validate_cursor_line``). The cursor can only land
    on a file row while that file is collapsed. Up/down from a child
    of an expanded parent skips the parent entirely; collapsing it
    makes the parent row selectable again."""
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        first_file = next(iter(tree.root.children))
        first_file.expand()
        await pilot.pause()
        tree.focus()
        await pilot.press("down")  # land on a leaf (parent is unselectable while expanded)
        await pilot.pause()
        assert tree.cursor_node is not first_file, "cursor must not land on an expanded file row"
        # Collapse the parent via Left. With the parent now collapsed,
        # the previously-visible child row is gone — cursor should
        # naturally end up on the parent row (which is now selectable).
        await pilot.press("left")
        await pilot.pause()
        assert not first_file.is_expanded
        assert tree.cursor_node is first_file

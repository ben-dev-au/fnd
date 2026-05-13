"""Regression tests for the three sidebar / preview bugs fixed in this branch:

* Preview pane stayed empty when the same cursor_line position was re-set
  after a follow-up query (``Tree.clear()`` doesn't reset cursor_line, and
  ``watch_cursor_line`` short-circuits when previous == current).
* Sidebar collapse state was being clobbered by the Tree's default
  ``auto_expand`` toggling collections back open every time the user
  pressed Enter to toggle their scope.
* Toggling a collection auto-reran the active query, which shifted focus
  to the results pane and made batch-toggling clumsy.
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


def _first_collection_node(ctree: Tree[dict[str, object]]):
    for n in ctree.root.children:
        if isinstance(n.data, dict) and n.data.get("kind") == "collection":
            return n
    return None


# ── Bug 1: preview must load on every query, not just the first ─────


@pytest.mark.asyncio
async def test_preview_loads_after_back_to_back_queries(built_index: Path) -> None:
    app = AcornApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._groups, "test setup — initial query produced no results"
        assert app._preview_parent_id is not None
        app._run_query("penguin")
        await pilot.pause()
        assert app._groups, "test setup — second query produced no results"
        assert app._preview_parent_id is not None, (
            "preview pane stayed empty on the second query — the cursor "
            "landed on the same line as the previous query and the "
            "NodeHighlighted event was suppressed"
        )


# ── Bug 2: collapse state persists across sessions ──────────────────


@pytest.mark.asyncio
async def test_panel_collapse_writes_to_disk(built_index: Path, isolated_ui_state: Path) -> None:
    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        ctree = app.query_one("#collections_panel_tree", Tree)
        ctree.focus()
        await pilot.pause()
        ctree.cursor_line = 0
        await pilot.pause()
        await pilot.press("left")
        await pilot.pause()
        assert "collapsed" in ctree.classes
        assert "collections_panel_tree" in app._collapsed_panels
        assert "collections_panel_tree" in isolated_ui_state.read_text()


@pytest.mark.asyncio
async def test_saved_collapse_state_is_restored_at_startup(
    built_index: Path, isolated_ui_state: Path
) -> None:
    isolated_ui_state.parent.mkdir(parents=True, exist_ok=True)
    isolated_ui_state.write_text(
        "[scope]\ncollections = []\nsources = []\n"
        "[panels]\n"
        'collapsed = ["collections_panel_tree", "filters_panel_tree"]\n'
        'expanded_collections = ["CPL"]\n'
        'expanded_filter_branches = ["kinds"]\n'
        '[filters]\nkinds = []\ndate = "any"\n'
    )
    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        ctree = app.query_one("#collections_panel_tree", Tree)
        ftree = app.query_one("#filters_panel_tree", Tree)
        assert "collapsed" in ctree.classes
        assert "collapsed" in ftree.classes
        for n in ctree.root.children:
            if isinstance(n.data, dict) and n.data.get("name") == "CPL":
                assert n.is_expanded
                break
        for n in ftree.root.children:
            if isinstance(n.data, dict) and n.data.get("category") == "kinds":
                assert n.is_expanded
                break


@pytest.mark.asyncio
async def test_enter_on_collection_does_not_undo_collapse(
    built_index: Path, isolated_ui_state: Path
) -> None:
    """The original symptom of Bug 2: pressing Enter to toggle a
    collection's scope used to also expand it (Tree.auto_expand=True),
    so an intentional Left-collapse was lost the next time the user
    toggled the collection's enable state."""
    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        ctree = app.query_one("#collections_panel_tree", Tree)
        coll = _first_collection_node(ctree)
        assert coll is not None
        coll.expand()
        await pilot.pause()
        coll.collapse()
        await pilot.pause()
        assert coll.data is not None
        assert coll.data["name"] not in app._expanded_collections
        ctree.focus()
        await pilot.pause()
        for i, n in enumerate(ctree.root.children):
            if n is coll:
                ctree.cursor_line = i
                break
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert not coll.is_expanded
        assert coll.data["name"] not in app._expanded_collections


# ── Bug 3: toggling a collection should not steal focus / rerun ─────


@pytest.mark.asyncio
async def test_toggle_with_active_query_clears_results_without_focus_shift(
    built_index: Path, isolated_ui_state: Path
) -> None:
    app = AcornApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._groups, "test setup — initial query produced no results"
        ctree = app.query_one("#collections_panel_tree", Tree)
        ctree.focus()
        await pilot.pause()
        assert app._focus_context() == "collections"
        for i, n in enumerate(ctree.root.children):
            if isinstance(n.data, dict) and n.data.get("kind") == "collection":
                ctree.cursor_line = i
                break
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app._focus_context() == "collections"
        assert app._groups == []


@pytest.mark.asyncio
async def test_collapse_state_survives_collection_cli_flag(
    built_index: Path, isolated_ui_state: Path
) -> None:
    """Bug C: launching with ``--collection X`` previously reset
    ``_collapsed_panels`` to empty, wiping the user's panel layout the
    next time they re-collapsed and re-saved. The flag overrides scope,
    not the sidebar's collapsed/expanded layout."""
    isolated_ui_state.parent.mkdir(parents=True, exist_ok=True)
    isolated_ui_state.write_text(
        '[scope]\ncollections = ["SFO"]\nsources = []\n'
        "[panels]\n"
        'collapsed = ["collections_panel_tree", "filters_panel_tree"]\n'
        'expanded_collections = ["CPL"]\n'
        'expanded_filter_branches = ["kinds"]\n'
        '[filters]\nkinds = []\ndate = "any"\n'
    )
    # Equivalent of ``acorn tui --collection default``: the flag pins
    # search scope to "default" but should NOT discard the saved
    # collapse-to-header state on the two sidebar panels.
    app = AcornApp(index_dir=built_index, collection="default")
    async with app.run_test() as pilot:
        await pilot.pause()
        ctree = app.query_one("#collections_panel_tree", Tree)
        ftree = app.query_one("#filters_panel_tree", Tree)
        # Scope override took effect.
        assert app._collections == ["default"]
        # Panel layout was restored from disk.
        assert "collapsed" in ctree.classes
        assert "collapsed" in ftree.classes
        assert app._collapsed_panels == {"collections_panel_tree", "filters_panel_tree"}
        assert "CPL" in app._expanded_collections
        assert "kinds" in app._expanded_filter_branches

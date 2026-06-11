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

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Tree

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp
from tests._pilot_wait import safe_pause, safe_press, settle, wait_until


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.fixture
def cfg(fixtures_dir: Path, tmp_path: Path) -> Config:
    """A self-contained config with three collections so the collections
    panel populates deterministically. These tests assert on the panel /
    saved-scope state, which is driven by the config — without an explicit
    one the app would read whatever config happens to be on disk, so it
    passed on a developer machine with collections configured and timed
    out on a clean CI runner with an empty config."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.default.sources]]
            path = "{fixtures_dir}"
            [[collections.alpha.sources]]
            path = "{tmp_path / "alpha"}"
            [[collections.beta.sources]]
            path = "{tmp_path / "beta"}"
        """),
        encoding="utf-8",
    )
    return load(cfg_path)


def _first_collection_node(ctree: Tree[dict[str, object]]):
    for n in ctree.root.children:
        if isinstance(n.data, dict) and n.data.get("kind") == "collection":
            return n
    return None


# ── Bug 1: preview must load on every query, not just the first ─────


@pytest.mark.asyncio
async def test_preview_loads_after_back_to_back_queries(built_index: Path) -> None:
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._search.groups, "test setup — initial query produced no results"
        assert app._preview.parent_id is not None
        app._search.run("penguin")
        await pilot.pause()
        assert app._search.groups, "test setup — second query produced no results"
        assert app._preview.parent_id is not None, (
            "preview pane stayed empty on the second query — the cursor "
            "landed on the same line as the previous query and the "
            "NodeHighlighted event was suppressed"
        )


# ── Bug 2: collapse state persists across sessions ──────────────────


@pytest.mark.asyncio
async def test_panel_collapse_writes_to_disk(
    built_index: Path, cfg: Config, isolated_ui_state: Path
) -> None:
    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await safe_pause(pilot)
        ctree = app.query_one("#collections_panel_tree", Tree)
        ctree.focus()
        await settle(pilot)
        ctree.cursor_line = 0
        await safe_pause(pilot)
        await safe_press(pilot, "left")
        await wait_until(
            pilot,
            lambda: "collapsed" in ctree.classes,
            timeout=30.0,
            message="ctree never gained 'collapsed' class",
        )
        assert "collections_panel_tree" in app._scope.collapsed_panels
        assert "collections_panel_tree" in isolated_ui_state.read_text()


@pytest.mark.asyncio
async def test_saved_collapse_state_is_restored_at_startup(
    built_index: Path, cfg: Config, isolated_ui_state: Path
) -> None:
    isolated_ui_state.parent.mkdir(parents=True, exist_ok=True)
    isolated_ui_state.write_text(
        "[scope]\ncollections = []\nsources = []\n"
        "[panels]\n"
        'collapsed = ["collections_panel_tree", "filters_panel_tree"]\n'
        'expanded_collections = ["alpha"]\n'
        'expanded_filter_branches = ["kinds"]\n'
        '[filters]\nkinds = []\ndate = "any"\n'
    )
    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        ctree = app.query_one("#collections_panel_tree", Tree)
        ftree = app.query_one("#filters_panel_tree", Tree)
        assert "collapsed" in ctree.classes
        assert "collapsed" in ftree.classes
        for n in ctree.root.children:
            if isinstance(n.data, dict) and n.data.get("name") == "alpha":
                assert n.is_expanded
                break
        for n in ftree.root.children:
            if isinstance(n.data, dict) and n.data.get("category") == "kinds":
                assert n.is_expanded
                break


@pytest.mark.asyncio
async def test_enter_on_collection_does_not_undo_collapse(
    built_index: Path, cfg: Config, isolated_ui_state: Path
) -> None:
    """The original symptom of Bug 2: pressing Enter to toggle a
    collection's scope used to also expand it (Tree.auto_expand=True),
    so an intentional Left-collapse was lost the next time the user
    toggled the collection's enable state."""
    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await safe_pause(pilot)
        ctree = app.query_one("#collections_panel_tree", Tree)
        await wait_until(
            pilot,
            lambda: _first_collection_node(ctree) is not None,
            timeout=30.0,
            message="collections tree never populated",
        )
        coll = _first_collection_node(ctree)
        assert coll is not None
        coll.expand()
        await safe_pause(pilot)
        coll.collapse()
        await safe_pause(pilot)
        assert coll.data is not None
        assert coll.data["name"] not in app._scope.expanded_collections
        ctree.focus()
        await settle(pilot)
        for i, n in enumerate(ctree.root.children):
            if n is coll:
                ctree.cursor_line = i
                break
        await safe_pause(pilot)
        await safe_press(pilot, "enter")
        await settle(pilot)
        assert not coll.is_expanded
        assert coll.data["name"] not in app._scope.expanded_collections


# ── Bug 3: toggling a collection should not steal focus / rerun ─────


@pytest.mark.asyncio
async def test_toggle_with_active_query_clears_results_without_focus_shift(
    built_index: Path, cfg: Config, isolated_ui_state: Path
) -> None:
    app = FNDApp(index_dir=built_index, config=cfg, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await safe_pause(pilot)
        assert app._search.groups, "test setup — initial query produced no results"
        ctree = app.query_one("#collections_panel_tree", Tree)
        ctree.focus()
        await settle(pilot)
        assert app._focus_context() == "collections"
        for i, n in enumerate(ctree.root.children):
            if isinstance(n.data, dict) and n.data.get("kind") == "collection":
                ctree.cursor_line = i
                break
        await safe_pause(pilot)
        await safe_press(pilot, "enter")
        await wait_until(
            pilot,
            lambda: app._search.groups == [],
            timeout=30.0,
            message="toggling collection did not clear results",
        )
        assert app._focus_context() == "collections"


@pytest.mark.asyncio
async def test_collapse_state_survives_collection_cli_flag(
    built_index: Path, cfg: Config, isolated_ui_state: Path
) -> None:
    """Bug C: launching with ``--collection X`` previously reset
    ``_collapsed_panels`` to empty, wiping the user's panel layout the
    next time they re-collapsed and re-saved. The flag overrides scope,
    not the sidebar's collapsed/expanded layout."""
    isolated_ui_state.parent.mkdir(parents=True, exist_ok=True)
    isolated_ui_state.write_text(
        '[scope]\ncollections = ["beta"]\nsources = []\n'
        "[panels]\n"
        'collapsed = ["collections_panel_tree", "filters_panel_tree"]\n'
        'expanded_collections = ["alpha"]\n'
        'expanded_filter_branches = ["kinds"]\n'
        '[filters]\nkinds = []\ndate = "any"\n'
    )
    # Equivalent of ``fnd tui --collection default``: the flag pins
    # search scope to "default" but should NOT discard the saved
    # collapse-to-header state on the two sidebar panels.
    app = FNDApp(index_dir=built_index, config=cfg, collection="default")
    async with app.run_test() as pilot:
        await settle(pilot)
        ctree = app.query_one("#collections_panel_tree", Tree)
        ftree = app.query_one("#filters_panel_tree", Tree)
        # Scope override took effect.
        assert app._scope.collections == ["default"]
        # Panel layout was restored from disk — wait for the saved
        # collapsed state to settle onto the trees, including the
        # expanded_collections / expanded_filter_branches lists which
        # restore through a separate async path that can lag the
        # collapse classes under CI load.
        await wait_until(
            pilot,
            lambda: (
                "collapsed" in ctree.classes
                and "collapsed" in ftree.classes
                and "alpha" in app._scope.expanded_collections
                and "kinds" in app._scope.expanded_filter_branches
            ),
            timeout=30.0,
            message="saved sidebar state not fully restored",
        )
        assert app._scope.collapsed_panels == {"collections_panel_tree", "filters_panel_tree"}

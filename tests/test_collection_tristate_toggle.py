"""Bug B: Collection toggle should propagate to sources, and the
collection marker should indicate full/partial/empty source state."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Tree

from fnd.config import CollectionConfig, Config, SourceConfig
from fnd.index import build_index
from fnd.tui import FNDApp


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.fixture
def multi_source_config(fixtures_dir: Path) -> Config:
    """A config with one collection that has two sources."""
    return Config(
        collections={
            "TWO": CollectionConfig(
                sources=[
                    SourceConfig(path=fixtures_dir / "notes"),
                    SourceConfig(path=fixtures_dir / "papers"),
                ],
            ),
        }
    )


def _first_collection_node(ctree: Tree[dict[str, object]]):
    for n in ctree.root.children:
        if isinstance(n.data, dict) and n.data.get("kind") == "collection":
            return n
    return None


@pytest.mark.asyncio
async def test_toggling_collection_on_marks_all_sources(
    built_index: Path, multi_source_config: Config, isolated_ui_state: Path
) -> None:
    app = FNDApp(index_dir=built_index, config=multi_source_config)
    async with app.run_test() as pilot:
        await pilot.pause()
        ctree = app.query_one("#collections_panel_tree", Tree)
        coll = _first_collection_node(ctree)
        assert coll is not None
        coll.expand()
        await pilot.pause()
        # Pre-condition: no sources active
        assert len(app._active_sources) == 0
        # Cursor on the collection row, press Enter
        ctree.focus()
        await pilot.pause()
        ctree.cursor_line = 0
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        # After toggle: all sources should be in _active_sources
        assert len(app._active_sources) == 2, (
            f"expected both sources active, got {app._active_sources}"
        )
        # Collection marker should be ● (full)
        assert "●" in str(coll.label), f"collection label was {coll.label}"
        # Each source row marker should be ●
        for src_node in coll.children:
            assert "●" in str(src_node.label), f"source label was {src_node.label}"


@pytest.mark.asyncio
async def test_single_source_toggle_marks_collection_partial(
    built_index: Path, multi_source_config: Config, isolated_ui_state: Path
) -> None:
    app = FNDApp(index_dir=built_index, config=multi_source_config)
    async with app.run_test() as pilot:
        await pilot.pause()
        ctree = app.query_one("#collections_panel_tree", Tree)
        coll = _first_collection_node(ctree)
        assert coll is not None
        coll.expand()
        await pilot.pause()
        ctree.focus()
        await pilot.pause()
        # Cursor on first source row (line 1 after collection at line 0)
        ctree.cursor_line = 1
        await pilot.pause()
        cursor = ctree.cursor_node
        assert cursor is not None
        assert isinstance(cursor.data, dict)
        assert cursor.data.get("kind") == "source", f"cursor on {cursor.data}"
        await pilot.press("enter")
        await pilot.pause()
        # One source on, one off → collection should be partial (◐)
        assert len(app._active_sources) == 1
        assert "◐" in str(coll.label), (
            f"expected partial marker, collection label was {coll.label!r}"
        )


@pytest.mark.asyncio
async def test_all_sources_toggled_individually_marks_collection_full(
    built_index: Path, multi_source_config: Config, isolated_ui_state: Path
) -> None:
    app = FNDApp(index_dir=built_index, config=multi_source_config)
    async with app.run_test() as pilot:
        await pilot.pause()
        ctree = app.query_one("#collections_panel_tree", Tree)
        coll = _first_collection_node(ctree)
        assert coll is not None
        coll.expand()
        await pilot.pause()
        ctree.focus()
        await pilot.pause()
        # Toggle on each source individually
        ctree.cursor_line = 1
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        ctree.cursor_line = 2
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        # Both sources on → collection marker should be ●
        assert "●" in str(coll.label), f"expected full marker, got {coll.label!r}"
        assert "◐" not in str(coll.label), f"expected no partial marker, got {coll.label!r}"


@pytest.mark.asyncio
async def test_toggle_collection_off_clears_sources(
    built_index: Path, multi_source_config: Config, isolated_ui_state: Path
) -> None:
    app = FNDApp(index_dir=built_index, config=multi_source_config)
    async with app.run_test() as pilot:
        await pilot.pause()
        ctree = app.query_one("#collections_panel_tree", Tree)
        coll = _first_collection_node(ctree)
        assert coll is not None
        coll.expand()
        await pilot.pause()
        ctree.focus()
        await pilot.pause()
        # Turn collection on
        ctree.cursor_line = 0
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert len(app._active_sources) == 2
        # Turn collection off
        await pilot.press("enter")
        await pilot.pause()
        assert len(app._active_sources) == 0
        assert "○" in str(coll.label), f"expected empty marker, got {coll.label!r}"


@pytest.mark.asyncio
async def test_cli_collection_shows_full_marker(
    built_index: Path, multi_source_config: Config, isolated_ui_state: Path
) -> None:
    """Launching with ``--collection TWO`` should display TWO as
    fully active (●) — every source toggled on — not as empty (○)."""
    app = FNDApp(index_dir=built_index, collection="TWO", config=multi_source_config)
    async with app.run_test() as pilot:
        await pilot.pause()
        ctree = app.query_one("#collections_panel_tree", Tree)
        # The seeded collection should be in scope.
        assert "TWO" in app._collections
        # Its sources should also be marked active so the tri-state
        # marker reads ● (full), not ○ (no sources active).
        coll = _first_collection_node(ctree)
        assert coll is not None
        assert "●" in str(coll.label), f"--collection X should mark TWO as ●; got {coll.label!r}"
        # And the source children of TWO must read ● too.
        coll.expand()
        await pilot.pause()
        for child in coll.children:
            assert "●" in str(child.label), (
                f"--collection X should fully activate sources; got {child.label!r}"
            )


@pytest.mark.asyncio
async def test_legacy_scope_only_collections_renders_as_full(
    built_index: Path, multi_source_config: Config, isolated_ui_state: Path
) -> None:
    """A saved state from before the tri-state rework — where
    ``_collections=["TWO"]`` but ``_active_sources=[]`` — should
    *render* as fully active (●) on every row, even though
    ``_active_sources`` stays empty in memory (we don't auto-expand
    it because the config / index might disagree on source paths)."""
    isolated_ui_state.parent.mkdir(parents=True, exist_ok=True)
    isolated_ui_state.write_text(
        '[scope]\ncollections = ["TWO"]\nsources = []\n'
        "[panels]\ncollapsed = []\nexpanded_collections = []\nexpanded_filter_branches = []\n"
        '[filters]\nkinds = []\ndate = "any"\n'
    )
    app = FNDApp(index_dir=built_index, config=multi_source_config)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "TWO" in app._collections
        ctree = app.query_one("#collections_panel_tree", Tree)
        coll = _first_collection_node(ctree)
        assert coll is not None
        assert "●" in str(coll.label), f"collection label was {coll.label!r}"
        coll.expand()
        await pilot.pause()
        for child in coll.children:
            assert "●" in str(child.label), f"source label was {child.label!r}"


@pytest.mark.asyncio
async def test_toggle_collection_off_from_cli_state(
    built_index: Path, multi_source_config: Config, isolated_ui_state: Path
) -> None:
    """Bug B v2 — pressing Enter on a CLI-flag collection (rendered ●
    with empty ``_active_sources``) must turn it off, not flip into
    the "fully on with sources" state silently."""
    app = FNDApp(index_dir=built_index, collection="TWO", config=multi_source_config)
    async with app.run_test() as pilot:
        await pilot.pause()
        ctree = app.query_one("#collections_panel_tree", Tree)
        coll = _first_collection_node(ctree)
        assert coll is not None
        assert "●" in str(coll.label)
        ctree.focus()
        await pilot.pause()
        ctree.cursor_line = 0
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert "TWO" not in app._collections
        assert app._active_sources == []
        assert "○" in str(coll.label), f"collection label was {coll.label!r}"

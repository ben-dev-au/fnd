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
        assert len(app._scope.active_sources) == 0
        # Cursor on the collection row, press Enter
        ctree.focus()
        await pilot.pause()
        ctree.cursor_line = 0
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        # After toggle the whole collection is FULL — it scopes via the
        # collection filter, so it appears in ``collections`` (not by
        # enumerating its source ids into ``active_sources``).
        assert "TWO" in app._scope.collections, f"expected TWO full, got {app._scope.collections}"
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
        assert len(app._scope.active_sources) == 1
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
        assert "TWO" in app._scope.collections
        # Turn collection off
        await pilot.press("enter")
        await pilot.pause()
        assert "TWO" not in app._scope.collections
        assert app._scope.active_sources == []
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
        assert "TWO" in app._scope.collections
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
        assert "TWO" in app._scope.collections
        ctree = app.query_one("#collections_panel_tree", Tree)
        coll = _first_collection_node(ctree)
        assert coll is not None
        assert "●" in str(coll.label), f"collection label was {coll.label!r}"
        coll.expand()
        await pilot.pause()
        for child in coll.children:
            assert "●" in str(child.label), f"source label was {child.label!r}"


@pytest.fixture
def shared_source_config(fixtures_dir: Path) -> Config:
    """Two collections that both include the same source path (the
    CPL/SFO Obsidian-vault shape): one private source each, plus a
    shared one whose resolved id is identical in both."""
    return Config(
        collections={
            "AAA": CollectionConfig(
                sources=[
                    SourceConfig(path=fixtures_dir / "notes"),
                    SourceConfig(path=fixtures_dir / "vault"),
                ],
            ),
            "BBB": CollectionConfig(
                sources=[
                    SourceConfig(path=fixtures_dir / "papers"),
                    SourceConfig(path=fixtures_dir / "vault"),
                ],
            ),
        }
    )


@pytest.mark.asyncio
async def test_collection_off_keeps_shared_source_of_active_sibling(
    built_index: Path, shared_source_config: Config, isolated_ui_state: Path
) -> None:
    """Toggling a collection OFF must not deactivate a source it shares
    with a collection that is still fully on. Regression: turning CPL
    off stripped the shared Obsidian vault from SFO's scope while SFO
    kept its ● marker, so SFO searches silently lost every md file.

    Both collections FULL scope via the collection filter, so survival
    is observable as BBB staying ● (FULL) after AAA toggles off — its
    shared vault is still in scope through BBB's collection channel."""
    app = FNDApp(index_dir=built_index, config=shared_source_config)
    async with app.run_test() as pilot:
        await pilot.pause()
        ctree = app.query_one("#collections_panel_tree", Tree)
        ctree.focus()
        await pilot.pause()
        # Rows 0/1 are AAA/BBB (collapsed). Toggle both fully on.
        ctree.cursor_line = 0
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        ctree.cursor_line = 1
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert sorted(app._scope.collections) == ["AAA", "BBB"]
        # Toggle AAA off.
        ctree.cursor_line = 0
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app._scope.collections == ["BBB"]
        assert app._scope.collection_marker("BBB") == "●", "BBB must stay fully scoped"
        vault_id = app._scope.collection_source_ids("BBB")[1]
        assert app._scope._source_active("BBB", vault_id), (
            "shared source must survive the sibling collection's toggle-off"
        )


@pytest.mark.asyncio
async def test_saved_scope_desync_repaired_on_launch(
    built_index: Path, shared_source_config: Config, isolated_ui_state: Path
) -> None:
    """A persisted scope where a collection is in ``collections`` but
    only some of its sources are in ``sources`` (written by the
    shared-source bug above) must read as FULL at launch: a full
    collection is config-relative, so it covers every current source
    and the marker reads ● regardless of the stale ``sources`` list."""
    notes_id = str((Path(__file__).parent / "fixtures" / "notes").resolve())
    isolated_ui_state.parent.mkdir(parents=True, exist_ok=True)
    isolated_ui_state.write_text(
        f'[scope]\ncollections = ["AAA"]\nsources = ["{notes_id}"]\n'
        "[panels]\ncollapsed = []\nexpanded_collections = []\nexpanded_filter_branches = []\n"
        '[filters]\nkinds = []\ndate = "any"\n'
    )
    app = FNDApp(index_dir=built_index, config=shared_source_config)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._scope.collections == ["AAA"]
        assert app._scope.collection_marker("AAA") == "●", "full collection must render ●"
        assert all(
            app._scope._source_active("AAA", sid)
            for sid in app._scope.collection_source_ids("AAA")
        ), "every source of a FULL collection is active"


def _source_node(coll_node, basename: str):
    """Find the source leaf under a collection node by path basename."""
    for child in coll_node.children:
        data = child.data if isinstance(child.data, dict) else {}
        if str(data.get("source_id", "")).rstrip("/").endswith(basename):
            return child
    return None


@pytest.mark.asyncio
async def test_collection_off_keeps_shared_source_of_partial_sibling(
    built_index: Path, shared_source_config: Config, isolated_ui_state: Path
) -> None:
    """#63: AAA full + BBB *partial* (only the shared vault on). Toggling
    AAA off must not strip the vault — BBB still claims it, so BBB stays
    ◐ and the vault row under BBB stays ●. The flat ``active_sources``
    list had no record of BBB's partial claim, so it was pruned."""
    app = FNDApp(index_dir=built_index, config=shared_source_config)
    async with app.run_test() as pilot:
        await pilot.pause()
        ctree = app.query_one("#collections_panel_tree", Tree)
        ctree.focus()
        await pilot.pause()
        # Make BBB partial first: expand it, toggle ONLY its vault source.
        bbb = ctree.root.children[1]
        assert isinstance(bbb.data, dict) and bbb.data.get("name") == "BBB"
        bbb.expand()
        await pilot.pause()
        vault_row = _source_node(bbb, "vault")
        assert vault_row is not None and vault_row.line >= 0
        ctree.cursor_line = vault_row.line
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        vault_id = app._scope.collection_source_ids("BBB")[1]
        assert vault_id in app._scope.active_sources
        assert "◐" in str(bbb.label), f"BBB should be partial, got {bbb.label!r}"
        # Now toggle AAA fully on.
        ctree.cursor_line = 0
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert "AAA" in app._scope.collections
        # Toggle AAA off — the shared vault must survive (BBB still claims it).
        ctree.cursor_line = 0
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert "AAA" not in app._scope.collections
        assert vault_id in app._scope.active_sources, (
            "shared source must survive a FULL sibling's toggle-off when a "
            f"PARTIAL sibling still claims it; got {app._scope.active_sources}"
        )
        assert "◐" in str(bbb.label), f"BBB lost its partial claim: {bbb.label!r}"
        assert "●" in str(_source_node(bbb, "vault").label)


@pytest.mark.asyncio
async def test_panel_title_source_count_agrees_with_markers(
    built_index: Path, multi_source_config: Config, isolated_ui_state: Path
) -> None:
    """#58: the toggle-time title refresh must count sources by the same
    rule the row markers use (``collection_full or id in active_sources``).
    A CLI ``--collection`` full collection paints every row ● but, with
    the old rule, the toggle-path title reported 0 sources — disagreeing
    with the full-rebuild title."""
    app = FNDApp(index_dir=built_index, collection="TWO", config=multi_source_config)
    async with app.run_test() as pilot:
        await pilot.pause()
        ctree = app.query_one("#collections_panel_tree", Tree)
        # Full rebuild computes the title alongside the ● row markers.
        app._scope.refresh_collections_panel()
        title_rebuild = str(ctree.border_title)
        # Toggle-path refresh must produce the same title for the same state.
        app._scope._refresh_collections_panel_title()
        title_toggle = str(ctree.border_title)
        assert title_toggle == title_rebuild, (
            f"title disagreement: rebuild={title_rebuild!r} toggle={title_toggle!r}"
        )
        assert "2/2 sources" in title_toggle, (
            f"CLI-full TWO should report all sources active; got {title_toggle!r}"
        )


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
        assert "TWO" not in app._scope.collections
        assert app._scope.active_sources == []
        assert "○" in str(coll.label), f"collection label was {coll.label!r}"

"""UX-D: collapsable collections panel below the results pane.

Lazygit-style sidebar: collections + sources visible in a tree under
the main results area. Enter on a collection toggles its activation
in the search scope. Section can collapse to header-only via Left
arrow on the panel root.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Tree

from acorn.config import Config, load
from acorn.index import build_index
from acorn.tui import AcornApp


def _write_md(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture
def cfg_two_collections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.papers.sources]]
            path = "/tmp/papers"

            [[collections.notes.sources]]
            path = "/tmp/notes"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("acorn.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.fixture
def two_collection_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    a = tmp_path / "papers"
    b = tmp_path / "notes"
    _write_md(a / "a.md", "# A\nshared anchor: glimmer\n")
    _write_md(b / "b.md", "# B\nshared anchor: glimmer\n")
    build_index(roots=[a], index_dir=tmp_index_dir, collection="papers")
    build_index(roots=[b], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_collections_panel_mounts_below_results(
    cfg_two_collections: Config, two_collection_index: Path
) -> None:
    """The panel exists in the DOM at startup with all configured
    collections visible as tree nodes."""
    app = AcornApp(index_dir=two_collection_index, config=cfg_two_collections)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#collections_panel_tree", Tree)
        labels = [str(c.label) for c in tree.root.children]
        text = "\n".join(labels)
        assert "papers" in text, text
        assert "notes" in text, text


@pytest.mark.asyncio
async def test_enter_toggles_collection_scope(
    cfg_two_collections: Config, two_collection_index: Path
) -> None:
    """Enter on a collection node in the panel should toggle its
    membership in the active search scope (per the user's explicit
    request: Enter, not Space)."""
    app = AcornApp(index_dir=two_collection_index, config=cfg_two_collections)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Start with no scope active.
        assert app._collections == []
        tree = app.query_one("#collections_panel_tree", Tree)
        tree.focus()
        await pilot.pause()
        # Cursor lands on first collection; press Enter to toggle on.
        await pilot.press("enter")
        await pilot.pause()
        # Whichever collection is first alphabetically should now be active.
        assert len(app._collections) == 1, app._collections
        # Press Enter again — should toggle off.
        await pilot.press("enter")
        await pilot.pause()
        assert app._collections == []


@pytest.mark.asyncio
async def test_active_collection_marked_in_label(
    cfg_two_collections: Config, two_collection_index: Path
) -> None:
    """Active collections should be visually marked in the tree label
    so the user can see at a glance which are in scope."""
    app = AcornApp(index_dir=two_collection_index, config=cfg_two_collections)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#collections_panel_tree", Tree)
        tree.focus()
        await pilot.pause()
        await pilot.press("enter")  # activate first collection
        await pilot.pause()
        first = next(iter(tree.root.children))
        label = str(first.label)
        # Active marker — using ● for active, ○ for inactive.
        assert "●" in label, f"active collection should be marked: {label!r}"


@pytest.mark.asyncio
async def test_panel_header_shows_active_count(
    cfg_two_collections: Config, two_collection_index: Path
) -> None:
    """The X/Y active count lives in the tree's ``border_title`` (matches
    the results pane's styling). Originally this was a separate Static
    above the tree — moved into the border title to make both panels
    look identical."""
    app = AcornApp(index_dir=two_collection_index, config=cfg_two_collections)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#collections_panel_tree", Tree)
        title = str(tree.border_title or "")
        assert "0/2" in title, title
        tree.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        title = str(tree.border_title or "")
        assert "1/2" in title, title

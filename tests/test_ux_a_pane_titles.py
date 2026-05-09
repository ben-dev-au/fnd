"""UX-A: focus-aware pane borders and informative border titles."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Tree

from acorn.index import build_index
from acorn.tui import AcornApp


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_results_pane_title_shows_counts_after_search(built_index: Path) -> None:
    """The results pane border title carries the file/section counts so the
    user always sees how many matches exist alongside the data, not buried
    in a separate status bar."""
    app = AcornApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        title = str(tree.border_title or "")
        # Should mention both the count of files and the count of sections.
        assert "file" in title.lower(), title
        assert "section" in title.lower(), title


@pytest.mark.asyncio
async def test_results_pane_title_empty_state(built_index: Path) -> None:
    """Before any query, the title should still render (something like
    ``Results``) — not be empty or crash."""
    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        title = str(tree.border_title or "")
        assert title.strip(), "results pane should always have a title"


@pytest.mark.asyncio
async def test_preview_pane_title_shows_file_name_when_focused(built_index: Path) -> None:
    """When a result is focused, the preview pane's border title should
    show the file name (so the user knows which document they're looking at)."""
    app = AcornApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        first = next(iter(tree.root.children))
        first.expand()
        await pilot.pause()
        tree.focus()
        await pilot.press("down")
        await pilot.pause()

        preview = app.query_one("#preview_pane", VerticalScroll)
        title = str(preview.border_title or "")
        # The fixture file is "test.pdf" — the basename should appear.
        assert ".pdf" in title.lower() or ".md" in title.lower(), title


@pytest.mark.asyncio
async def test_status_bar_widget_is_removed(built_index: Path) -> None:
    """The top status bar is gone — its only content (active scope) is
    shown in the Collections panel border title instead."""
    app = AcornApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not app.query("#status_bar"), "status bar widget still mounted"

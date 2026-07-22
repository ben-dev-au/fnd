"""Results-pane collapse-to-header behaviour.

Bug 3: collapsing the pane while a lower result is selected must keep the
*selected* file in the single visible strip row — not snap back to the top
result (the strip drives the preview, so it must show the driving file).

Bug 4: clicking the collapsed strip must reopen the pane (and expand the
clicked result) rather than silently toggling a node the user can't see.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.pilot import Pilot

from fnd.config import load
from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.widgets.results_tree import ResultsTree


@pytest.fixture
def multi_result_app(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> FNDApp:
    docs = tmp_path / "docs"
    docs.mkdir()
    for i in range(10):
        (docs / f"file{i:02d}.md").write_text(
            f"# File {i:02d} Title\n\nglimmer content number {i} lorem ipsum.\n",
            encoding="utf-8",
        )
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.notes.sources]]
            path = "{docs.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    build_index(roots=[docs], index_dir=tmp_index_dir, collection="notes")
    return FNDApp(
        config=load(cfg_path),
        index_dir=tmp_index_dir,
        collection="notes",
        initial_query="glimmer",
    )


async def _query_and_select(app: FNDApp, pilot: Pilot[None], target_line: int) -> ResultsTree:
    for _ in range(8):
        await pilot.pause()
    tree = app.query_one("#results_pane", ResultsTree)
    tree.focus()
    # Collapse the auto-expanded top file so every file is a contiguous row.
    tree.root.children[0].collapse()
    await pilot.pause()
    tree.cursor_line = target_line
    await pilot.pause()
    return tree


@pytest.mark.asyncio
async def test_collapsing_keeps_selected_file_in_strip(multi_result_app: FNDApp) -> None:
    app = multi_result_app
    async with app.run_test(size=(120, 40)) as pilot:
        tree = await _query_and_select(app, pilot, target_line=5)
        assert tree.cursor_line == 5

        app.action_tree_smart_collapse()  # collapse the whole panel to its strip
        for _ in range(5):
            await pilot.pause()

        assert "collapsed" in tree.classes
        # The single visible content row is at scroll_offset.y; it must be the
        # selected (cursor) row, i.e. the file driving the preview.
        assert tree.scroll_offset.y == tree.cursor_line, (
            tree.scroll_offset.y,
            tree.cursor_line,
        )


@pytest.mark.asyncio
async def test_clicking_collapsed_strip_reopens_and_expands(multi_result_app: FNDApp) -> None:
    app = multi_result_app
    async with app.run_test(size=(120, 40)) as pilot:
        tree = await _query_and_select(app, pilot, target_line=3)
        app.action_tree_smart_collapse()
        for _ in range(5):
            await pilot.pause()
        assert "collapsed" in tree.classes

        visible = tree._tree_lines[tree.scroll_offset.y].node
        assert visible is not None
        assert not visible.is_expanded

        # Click the single visible content row (offset is widget-relative: past
        # the top border row, past the toggle column, onto the label).
        await pilot.click("#results_pane", offset=(4, 1))
        for _ in range(5):
            await pilot.pause()

        assert "collapsed" not in tree.classes, "click on collapsed strip should reopen the pane"
        assert visible.is_expanded, "the clicked result should expand too"

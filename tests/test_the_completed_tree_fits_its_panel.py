"""The Completed tree painted over the modal's border and cut a line mid-word.

Measured at 110 columns with six finished collections: the texturising line
read `⚠ 1 still fl` against the panel edge, and a horizontal scrollbar painted
a row of dashes inside the tree that reads as a broken row.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Tree

from fnd.tui import FNDApp
from fnd.tui.indexer_modal import ChainStepSummary, IndexerScreen


def _summary(collection: str) -> ChainStepSummary:
    return ChainStepSummary(
        collection=collection,
        files_total=352,
        pdfs_total=4,
        indexed_newly=12,
        indexed_already=340,
        textured_newly=2,
        textured_already=1,
        still_flat=1,
        failed=1,
        removed=3,
        elapsed_s=1.0,
    )


async def _open(app: FNDApp, pilot: Any, names: tuple[str, ...]) -> Any:
    app._indexer.chain_history = [_summary(n) for n in names]
    app.push_screen(IndexerScreen(names[0]))
    for _ in range(20):
        await pilot.pause()
    screen = app.screen
    assert isinstance(screen, IndexerScreen)
    screen._refresh_history_band()
    screen.query_one("#indexer_history_tree", Tree).root.expand()
    for _ in range(10):
        await pilot.pause()
    return screen


@pytest.mark.asyncio
async def test_a_long_row_is_not_cut_mid_word(tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot, ("notes", "papers", "research", "wine", "archive", "dpc"))
        rows = ["".join(s.text for s in strip) for strip in screen._compositor.render_strips()]

    # The tree row is compact: nested under the collection it names, it drops
    # the heading the modal's own line carries.
    texturising = [r for r in rows if "still flat" in r or ("already" in r and "new" in r)]
    assert texturising, "nothing painted"
    assert all("still flat" in r for r in texturising if "flat" in r or "1 already" in r), (
        texturising
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [80, 110])
async def test_it_does_not_paint_a_horizontal_scrollbar(width: int, tmp_index_dir: Path) -> None:
    """The dashes read as a broken tree row, not as a control.

    80 is where it bites: at 110 the narrower guides alone keep the rows
    inside the panel.
    """
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(width, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot, ("notes", "papers", "research", "wine", "archive", "dpc"))
        tree = screen.query_one("#indexer_history_tree", Tree)
        shown = tree.show_horizontal_scrollbar

    assert not shown, "the tree scrolls sideways, and paints a bar saying so"


@pytest.mark.asyncio
async def test_the_vertical_scrollbar_stays(tmp_index_dir: Path) -> None:
    """The control: more entries than fit must still say so."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot, ("notes", "papers", "research", "wine", "archive", "dpc"))
        tree = screen.query_one("#indexer_history_tree", Tree)
        shown = tree.show_vertical_scrollbar

    assert shown, "eighteen rows in a ten-row box with nothing to say so"


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [80, 110])
async def test_a_tree_row_keeps_its_still_flat_chip(width: int, tmp_index_dir: Path) -> None:
    """Hiding the scrollbar removed the only signal that a row had more.

    A Tree clips its labels rather than wrapping them, so `overflow-x: hidden`
    took away the indicator without shortening what overflows.
    """
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(width, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot, ("notes", "papers"))
        rows = ["".join(s.text for s in strip) for strip in screen._compositor.render_strips()]

    # The tree row is compact: nested under the collection it names, it drops
    # the heading the modal's own line carries.
    texturising = [r for r in rows if "still flat" in r or ("already" in r and "new" in r)]
    assert texturising, "nothing painted"
    assert all("still flat" in r for r in texturising if "flat" in r or "1 already" in r), (
        texturising
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [60, 80, 110])
async def test_a_tree_row_keeps_its_failed_chip(width: int, tmp_index_dir: Path) -> None:
    """The live status line wraps; the archived copy in the tree clips, and a
    failure count is the last thing that may silently vanish there.
    """
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(width, 34)) as pilot:
        await pilot.pause()
        screen = await _open(app, pilot, ("notes", "papers"))
        rows = ["".join(s.text for s in strip) for strip in screen._compositor.render_strips()]

    # Found by its label, not by the tail: `340 already` is deliberately
    # the first thing the clip eats, so it cannot be the row's selector.
    indexed = [r for r in rows if "Files" in r and "12 new" in r]
    assert indexed, "nothing painted"
    assert all("1 failed" in r for r in indexed), indexed

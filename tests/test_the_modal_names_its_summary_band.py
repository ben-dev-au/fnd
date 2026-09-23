"""The footer names the `Completed` band that holds the removed-file counts.

`↑↓ / ⏎ / Esc` drive the action list; the band beside it is focusable, and
without a footer entry only a guess at Shift+Tab then Space reaches it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Static, Tree

from fnd.tui import FNDApp
from fnd.tui.indexer_modal import ChainStepSummary, IndexerScreen


def _summary(collection: str) -> ChainStepSummary:
    return ChainStepSummary(
        collection=collection,
        files_total=8,
        pdfs_total=0,
        indexed_newly=0,
        indexed_already=8,
        textured_newly=0,
        textured_already=0,
        still_flat=0,
        failed=0,
        removed=3,
        elapsed_s=1.0,
    )


async def _modal(app: FNDApp, pilot: Any, *, history: bool) -> IndexerScreen:
    if history:
        app._indexer.chain_history = [_summary("notes")]  # type: ignore[attr-defined]
    app.push_screen(IndexerScreen("notes"))
    for _ in range(20):
        await pilot.pause()
    screen = app.screen
    assert isinstance(screen, IndexerScreen)
    screen._refresh_history_band()
    screen._sync_action_options(app)
    for _ in range(6):
        await pilot.pause()
    return screen


def _footer(screen: IndexerScreen) -> str:
    return str(screen.query_one("#footer_hints", Static).content)


@pytest.mark.asyncio
async def test_the_band_is_named_once_there_is_one(tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        screen = await _modal(app, pilot, history=True)
        shown = "hidden" not in screen.query_one("#indexer_history_tree", Tree).classes
        footer = _footer(screen)

    assert shown, "the band is on screen"
    assert "Completed" in footer, footer


@pytest.mark.asyncio
async def test_it_is_not_named_when_there_is_nothing_in_it(tmp_index_dir: Path) -> None:
    """The control: an empty band is hidden, and must not be advertised."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        screen = await _modal(app, pilot, history=False)
        hidden = "hidden" in screen.query_one("#indexer_history_tree", Tree).classes
        footer = _footer(screen)

    assert hidden
    assert "Completed" not in footer, footer


@pytest.mark.asyncio
async def test_the_key_it_names_reaches_the_band(tmp_index_dir: Path) -> None:
    """A chip for a key that does not get there would be the same defect."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        await _modal(app, pilot, history=True)
        for _ in range(4):
            await pilot.press("tab")
            for _ in range(3):
                await pilot.pause()
            if isinstance(app.focused, Tree):
                break
        landed = type(app.focused).__name__

    assert landed == "Tree", landed

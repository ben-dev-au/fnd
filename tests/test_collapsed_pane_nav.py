"""While a sidebar panel is collapsed-to-header, Up/Down navigate *between*
panels (up = previous, down = next, wrapping) rather than moving a cursor no
one can see. The filters tree lives inside the ``#filters_pane`` wrapper, so it
is not a direct child ``Tree`` of the column — the navigation must still reach
it, both to depart from and to arrive at. Regression: the wrapper hid the
filters tree from the pane list, so Up/Down stopped working there.
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


async def _collapse_all(app: FNDApp) -> None:
    for tid in ("results_pane", "collections_panel_tree", "filters_panel_tree"):
        app._panel_frame(app.query_one(f"#{tid}", Tree)).add_class("collapsed")


@pytest.mark.asyncio
async def test_up_departs_the_collapsed_filters_pane(built_index: Path) -> None:
    """The literal bug report: with the filters pane collapsed, Up moves focus
    to the previous panel (Collections) instead of doing nothing."""
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        await _collapse_all(app)
        app.query_one("#filters_panel_tree", Tree).focus()
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is app.query_one("#collections_panel_tree", Tree)


@pytest.mark.asyncio
async def test_down_wraps_from_collapsed_filters_to_results(built_index: Path) -> None:
    """Filters is the last panel; Down from it wraps to the first (Results)."""
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        await _collapse_all(app)
        app.query_one("#filters_panel_tree", Tree).focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is app.query_one("#results_pane", Tree)


@pytest.mark.asyncio
async def test_filters_pane_is_reachable_as_a_target(built_index: Path) -> None:
    """The wrapped filters tree must also be arrived at: Down from the
    collapsed Collections panel lands on it."""
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        await _collapse_all(app)
        app.query_one("#collections_panel_tree", Tree).focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is app.query_one("#filters_panel_tree", Tree)


@pytest.mark.asyncio
async def test_full_cycle_visits_every_panel_both_ways(built_index: Path) -> None:
    """A full Up sweep and a full Down sweep each visit all three panels,
    proving the filters pane is a first-class stop in the cycle."""
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        await _collapse_all(app)
        results = app.query_one("#results_pane", Tree)
        collections = app.query_one("#collections_panel_tree", Tree)
        filters = app.query_one("#filters_panel_tree", Tree)

        filters.focus()
        await pilot.pause()
        for expected in (collections, results, filters):  # Up = previous, wraps
            await pilot.press("up")
            await pilot.pause()
            assert app.focused is expected

        for expected in (results, collections, filters):  # Down = next, wraps
            await pilot.press("down")
            await pilot.pause()
            assert app.focused is expected


@pytest.mark.asyncio
async def test_uncollapsed_filters_pane_keeps_normal_arrow_behaviour(built_index: Path) -> None:
    """Guard the other side: when the filters pane is *not* collapsed, Down is
    an ordinary in-tree cursor move, not a jump to another panel."""
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        filters = app.query_one("#filters_panel_tree", Tree)
        filters.focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is filters, "Down in an open filters pane must not leave it"

"""A sample that lands after the filter browser has gone is let go.

The count runs on a worker and rebuilds the tree when it lands. Landing on a
screen whose tree had gone (closed or torn down mid-scan) raised NoMatches
inside the worker, and a failed worker takes the whole app down.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.filters import FilterSpec
from fnd.filters.scan import SourceSample
from fnd.tui import FNDApp
from fnd.tui.settings_screen import FilterBrowserScreen
from tests._pilot_wait import wait_until


@pytest.mark.asyncio
async def test_a_sample_landing_on_a_closed_browser_is_let_go(tmp_index_dir: Path) -> None:
    """Delivered after the screen's tree is gone, the sample raises nothing."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        screen = FilterBrowserScreen(
            title="Index filters",
            spec=FilterSpec(),
            gitignore=True,
            fndignore=True,
            sample_provider=lambda _spec: SourceSample(kinds={"md": 1}, tags={}),
            on_save=lambda *_a: None,
        )
        app.push_screen(screen)
        await wait_until(pilot, lambda: bool(screen.query("#filter_tree")), timeout=10)
        app.pop_screen()
        await wait_until(pilot, lambda: not screen.query("#filter_tree"), timeout=10)

        screen._sample_arrived(SourceSample(kinds={"md": 2}, tags={}), FilterSpec())

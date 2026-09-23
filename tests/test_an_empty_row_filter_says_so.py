"""A row filter matching nothing says so, rather than painting a blank pane.

Thirty blank lines and no message read as a hung process. The border title is
where this app already carries counts (the Results and Filters titles), so it
is where the absence of them belongs too.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.containers import Vertical

from fnd.filters import FilterSpec
from fnd.filters.scan import SourceSample
from fnd.tui import FNDApp
from fnd.tui.settings_screen import FilterBrowserScreen

_SAMPLE = SourceSample(kinds={"md": 3}, tags={"frontmatter": {"keep": 2}})


async def _browser(app: FNDApp, pilot: object) -> FilterBrowserScreen:
    screen = FilterBrowserScreen(
        title="Index filters",
        spec=FilterSpec(),
        gitignore=True,
        fndignore=True,
        sample_provider=lambda _spec: _SAMPLE,
        on_save=lambda *_a: None,
    )
    app.push_screen(screen)
    for _ in range(25):
        await pilot.pause()  # type: ignore[attr-defined]
    return screen


def _title(app: FNDApp) -> str:
    return str(app.screen.query_one("#settings_box", Vertical).border_title)


@pytest.mark.asyncio
async def test_it_names_the_query_that_found_nothing(tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        screen = await _browser(app, pilot)
        screen._query = "zzzznothingmatches"
        screen._rebuild(focus_tree=False)
        for _ in range(8):
            await pilot.pause()
        title = _title(app)
        on_screen = "\n".join(
            "".join(s.text for s in strip) for strip in app.screen._compositor.render_strips()
        )

    assert "no rows match" in title, title
    assert "zzzznothingmatches" in title, title
    assert "no rows match" in on_screen, "it never reached the screen"


@pytest.mark.asyncio
async def test_a_query_that_matches_says_nothing_extra(tmp_index_dir: Path) -> None:
    """The control: the title is the pane's name again the moment rows return,
    so a stale warning cannot outlive the query that caused it."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        screen = await _browser(app, pilot)
        screen._query = "zzzznothingmatches"
        screen._rebuild(focus_tree=False)
        for _ in range(6):
            await pilot.pause()
        assert "no rows match" in _title(app), "the premise"
        screen._query = "md"
        screen._rebuild(focus_tree=False)
        for _ in range(6):
            await pilot.pause()
        title = _title(app)

    assert title == "Index filters", title


@pytest.mark.asyncio
async def test_an_unfiltered_pane_is_untouched(tmp_index_dir: Path) -> None:
    """The other control: no query, no claim about one."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        await _browser(app, pilot)
        title = _title(app)

    assert title == "Index filters", title

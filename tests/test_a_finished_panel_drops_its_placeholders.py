"""A finished run read `Current: ?` and `Avg: ?`.

`?` is honest while a run is going (the modal genuinely does not know the file
yet, or has no pages counted) and meaningless once it has stopped, where there
is no current file and a corpus without PDFs will never have an average.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Static

from fnd.tui import FNDApp
from fnd.tui.indexer_modal import IndexerScreen, _current_line, _short_name, fmt_per_page


def test_no_current_file_means_no_line() -> None:
    assert _current_line("") == ""


def test_a_current_file_still_names_it() -> None:
    """The control: the line exists for the running case."""
    line = _current_line("/vault/notes/alpha.md")

    assert "alpha.md" in line
    assert "Current:" in line


def test_the_stuck_suffix_survives() -> None:
    assert "· stuck" in _current_line("/a/b.pdf", " · stuck")


def test_the_placeholder_itself_is_unchanged() -> None:
    """`_short_name` is used elsewhere; this fix is about the line, not it."""
    assert _short_name("") == "?"


def test_an_average_of_nothing_is_not_shown() -> None:
    assert fmt_per_page(0, 0.0) == "?"


@pytest.mark.asyncio
async def test_a_panel_with_no_run_shows_neither(tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        app.push_screen(IndexerScreen("notes"))
        for _ in range(15):
            await pilot.pause()
        screen = app.screen
        assert isinstance(screen, IndexerScreen)
        screen._render_timing(1.0)
        # The current-file line is written by the pages-progress render, which
        # a headless run never reaches, so it is driven here; otherwise this
        # asserts on the empty string the widget was composed with.
        screen._render_pages_progress(app)
        await pilot.pause()
        current = str(screen.query_one("#indexer_current_file", Static).content)
        timing = str(screen.query_one("#indexer_timing", Static).content)

    assert "?" not in current, current
    assert "?" not in timing, timing
    assert "Elapsed" in timing, "the part that is known must stay"

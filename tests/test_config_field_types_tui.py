"""The Settings editors show the verdict the config model gives on save."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Static, TextArea

from fnd.filters import FilterSpec
from fnd.tui import FNDApp
from fnd.tui.settings_screen import FilterTextScreen, GlobTextScreen


async def _settle(pilot: Any, n: int = 10) -> None:
    for _ in range(n):
        await pilot.pause()


@pytest.mark.asyncio
async def test_a_glob_holding_a_comma_survives_an_unedited_esc(tmp_index_dir: Path) -> None:
    got: list[tuple[str, ...]] = []
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 30)) as pilot:
        await _settle(pilot, 5)
        app.push_screen(
            GlobTextScreen(title="Excluded globs", value=("report,final*.md",), on_save=got.append)
        )
        await _settle(pilot)
        await pilot.press("escape")
        await _settle(pilot, 4)
    assert got == []


@pytest.mark.asyncio
async def test_the_filter_editor_refuses_what_the_save_refuses(tmp_index_dir: Path) -> None:
    got: list[Any] = []
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 30)) as pilot:
        await _settle(pilot, 5)
        app.push_screen(FilterTextScreen(title="t", spec=FilterSpec(), on_save=got.append))
        await _settle(pilot)
        screen = app.screen
        assert isinstance(screen, FilterTextScreen)
        screen.query_one("#filter_text", TextArea).text = "file.kind in ['md', 'markdown']"
        await _settle(pilot, 6)
        status = str(screen.query_one("#filter_status", Static).render())
        refused = screen.hand_back()
    assert status.startswith("✗"), status
    assert "markdown" in refused
    assert got == []

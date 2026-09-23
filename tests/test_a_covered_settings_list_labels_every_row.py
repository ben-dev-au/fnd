"""A settings list rebuilt while its screen is covered labels every row it keeps."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Static

from fnd.config import load
from fnd.tui import FNDApp
from fnd.tui.menu import SECTION_COLLECTIONS, SECTION_PREFERENCES
from fnd.tui.settings_screen import SettingsList, SettingsScreen, open_settings_section
from tests._pilot_wait import settings_ready


@pytest.mark.asyncio
async def test_two_rebuilds_under_a_covering_screen_leave_no_row_blank(
    tmp_path: Path, tmp_index_dir: Path, isolated_config_path: Path
) -> None:
    """Rebuilds that land before the last batch of rows is pruned still label the live rows."""
    isolated_config_path.write_text(
        f'[[collections.alpha.sources]]\npath = "{tmp_path.as_posix()}"\n', encoding="utf-8"
    )
    app = FNDApp(index_dir=tmp_index_dir, config=load())
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_COLLECTIONS)
        await settings_ready(pilot, app)
        covered = app.screen
        assert isinstance(covered, SettingsScreen)
        open_settings_section(app, SECTION_PREFERENCES)
        await settings_ready(pilot, app)

        covered.refresh_items()
        covered.refresh_items()
        for _ in range(10):
            await pilot.pause()

        lst = covered.query_one(SettingsList)
        rows = list(lst.query_one("#settings_list_body", VerticalScroll).query(Static))
        painted = [str(row.content) for row in rows]

    assert len(painted) == len(lst._items), painted
    blank = [it.label for it, text in zip(lst._items, painted, strict=True) if it.label not in text]
    assert not blank, blank

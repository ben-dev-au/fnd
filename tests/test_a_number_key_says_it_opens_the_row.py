"""`1-9 Jump by index` moves the cursor and opens the row, and says so.

The opening is deliberate (`action_jump` posts `Activated` on purpose), so the
docstring and the cheat sheet must say a digit leaves for another screen.

If the opening is ever decided to be wrong, this test changes with it: it
asserts the app and its description agree, not that the key must open.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from fnd.tui import FNDApp
from fnd.tui.menu import _provider_keybindings
from fnd.tui.settings_screen import SettingsList, SettingsScreen, open_settings


def _row_description() -> str:
    items = _provider_keybindings(cast("Any", SimpleNamespace(_config=None)))
    return next(i.description for i in items if not i.is_header and i.key == "1-9")


@pytest.mark.asyncio
async def test_a_digit_opens_the_row_it_jumps_to(tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        open_settings(app)
        for _ in range(20):
            await pilot.pause()
        opened_on = app.screen
        assert isinstance(opened_on, SettingsScreen)
        opened_on.query_one(SettingsList).focus()
        await pilot.pause()

        await pilot.press("3")
        for _ in range(8):
            await pilot.pause()
        moved_on = app.screen is not opened_on

    assert moved_on, "the digit did not open anything"


def test_the_sheet_says_it_opens() -> None:
    """ "open" alone is satisfied by "a settings screen opens…" further along
    the same sentence; the assertion has to name the key's own effect."""
    description = _row_description()

    assert "open it" in description.lower(), description

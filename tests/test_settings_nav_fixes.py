"""Settings navigation fixes:

* Bug A — searching is navigation-only: Enter lands on the first match and
  does NOT fire its effect (no toggle flip, no drill, no side-effect).
* Bug B — returning from a drilled child screen keeps the cursor on the row
  the user drilled from, not reset to the first row.
"""

from __future__ import annotations

import pytest
from textual.pilot import Pilot
from textual.widgets import Input

from fnd.tui import FNDApp
from fnd.tui.menu import KIND_PICKER, KIND_SUBMENU, KIND_TOGGLE
from fnd.tui.settings_screen import SettingsList, SettingsScreen
from tests._pilot_wait import settings_ready


async def _open_settings(app: FNDApp, pilot: Pilot[None]) -> SettingsScreen:
    await pilot.pause()
    app.action_open_command_palette()
    await settings_ready(pilot, app)
    screen = app.screen
    assert isinstance(screen, SettingsScreen)
    return screen


# ── Bug A: search is navigation-only ─────────────────────────────────


@pytest.mark.asyncio
async def test_search_enter_does_not_toggle() -> None:
    app = FNDApp()
    async with app.run_test() as pilot:
        screen = await _open_settings(app, pilot)
        search = screen.query_one("#settings_search", Input)
        search.value = "highlights"
        await pilot.pause()
        lst = screen.query_one(SettingsList)
        item = lst._items[0]
        assert item.kind == KIND_TOGGLE, item.kind
        assert item.toggle_getter is not None
        before = item.toggle_getter(app)
        await pilot.press("enter")  # search submit
        await pilot.pause()
        assert app.screen is screen, "must not drill/close"
        assert app.focused is lst, "focus moves to list"
        assert item.toggle_getter(app) == before, "toggle must NOT flip on search Enter"


@pytest.mark.asyncio
async def test_search_enter_does_not_drill_picker() -> None:
    app = FNDApp()
    async with app.run_test() as pilot:
        screen = await _open_settings(app, pilot)
        search = screen.query_one("#settings_search", Input)
        search.value = "default collection"
        await pilot.pause()
        lst = screen.query_one(SettingsList)
        assert lst._items[0].kind == KIND_PICKER, lst._items[0].kind
        await pilot.press("enter")
        await pilot.pause()
        # Old behaviour pushed a PickerScreen; navigate-only stays put.
        assert app.screen is screen, "search Enter must not drill into the picker"
        assert app.focused is lst


# ── Bug B: back-nav keeps cursor on the drilled-from row ─────────────


@pytest.mark.asyncio
async def test_back_from_child_keeps_cursor_on_drilled_row() -> None:
    app = FNDApp()
    async with app.run_test() as pilot:
        screen = await _open_settings(app, pilot)
        lst = screen.query_one(SettingsList)
        # Drill into Preferences (an external section row).
        pref_idx = next(i for i, it in enumerate(lst._items) if it.label == "Preferences")
        lst.focus()
        lst.cursor_index = pref_idx
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        prefs = app.screen
        assert isinstance(prefs, SettingsScreen)
        plst = prefs.query_one(SettingsList)
        # Pick a picker/submenu child to drill into.
        child_idx = next(
            i for i, it in enumerate(plst._items) if it.kind in {KIND_PICKER, KIND_SUBMENU}
        )
        child_id = plst._items[child_idx].id
        plst.cursor_index = child_idx
        await pilot.pause()
        await pilot.press("enter")  # drill into the child screen
        await pilot.pause()
        assert app.screen is not prefs, "should have drilled into a child screen"
        await pilot.press("left")  # back to Preferences
        await pilot.pause()
        back = app.screen
        assert isinstance(back, SettingsScreen)
        blst = back.query_one(SettingsList)
        cur_id = (
            blst._items[blst.cursor_index].id if 0 <= blst.cursor_index < len(blst._items) else None
        )
        assert cur_id == child_id, f"cursor should stay on {child_id}, got {cur_id}"
        assert app.focused is blst, "list (not the search box) should hold focus"

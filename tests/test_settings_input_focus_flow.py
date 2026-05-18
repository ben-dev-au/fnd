"""Settings menu opens with the filter Input focused, and arrow keys
bridge focus between the Input and the SettingsList."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Input

from fnd.index import build_index
from fnd.tui import FNDApp


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_root_menu_focuses_search_input(built_index: Path) -> None:
    """Root menu open: the search Input owns focus — typing immediately filters."""
    from fnd.tui.settings_screen import SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        assert isinstance(screen.focused, Input)
        assert screen.focused.id == "settings_search"


@pytest.mark.asyncio
async def test_submenu_open_focuses_list_not_input(built_index: Path) -> None:
    """Drilling into a sub-menu (e.g. Preferences) focuses the list —
    the user already chose what they wanted by drilling in. The filter
    Input is still reachable via `/` from any screen.
    """
    from fnd.tui.menu import SECTION_PREFERENCES
    from fnd.tui.settings_screen import (
        SettingsList,
        SettingsScreen,
        open_settings_section,
    )

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_PREFERENCES)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        assert screen._breadcrumb != ()  # sanity: this is a sub-screen
        assert isinstance(screen.focused, SettingsList)


@pytest.mark.asyncio
async def test_typing_immediately_routes_to_filter(built_index: Path) -> None:
    """No focus shift needed — typed chars land in the filter Input."""
    from fnd.tui.settings_screen import SettingsList, SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        before_items = list(screen.query_one(SettingsList)._items)
        await pilot.press("p")
        await pilot.pause()
        # The typed char reached the filter Input.
        search = screen.query_one("#settings_search", Input)
        assert search.value == "p"
        # And the list refreshed in response (cross-section search
        # widens the list — what matters is that filtering kicked in).
        after_items = list(screen.query_one(SettingsList)._items)
        assert after_items != before_items, "filter should refresh the list"


@pytest.mark.asyncio
async def test_down_from_input_focuses_list(built_index: Path) -> None:
    from fnd.tui.settings_screen import SettingsList, SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        await pilot.press("down")
        await pilot.pause()
        assert isinstance(screen.focused, SettingsList)


@pytest.mark.asyncio
async def test_up_at_top_of_list_focuses_input(built_index: Path) -> None:
    """Up at the topmost selectable row hands focus back to the Input."""
    from fnd.tui.settings_screen import SettingsList, SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        # Move into the list.
        await pilot.press("down")
        await pilot.pause()
        lst = screen.query_one(SettingsList)
        assert isinstance(screen.focused, SettingsList)
        # Force cursor to the first selectable row (mount already does
        # this, but make it explicit so the test pins behaviour).
        first = lst._first_selectable(0, +1)
        assert first is not None
        lst.cursor_index = first
        await pilot.press("up")
        await pilot.pause()
        assert isinstance(screen.focused, Input)
        assert screen.focused.id == "settings_search"


@pytest.mark.asyncio
async def test_no_cursor_highlight_while_input_focused(built_index: Path) -> None:
    """When the filter Input owns focus there must not be a visible
    cursor highlight competing with it. The `.-cursor` class still
    *exists* on the underlying row (so refocusing the list snaps the
    eye to where the cursor sits), but CSS suppresses the paint
    behind ``SettingsList:focus``.
    """
    from textual.widgets import Static

    from fnd.tui.settings_screen import SettingsList, SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        assert isinstance(screen.focused, Input)
        # The list is not focused.
        lst = screen.query_one(SettingsList)
        assert not lst.has_focus
        # A cursor row exists on the DOM (the data is intact)…
        rows = list(lst.query("Static.row"))
        cursor_rows = [r for r in rows if isinstance(r, Static) and "-cursor" in r.classes]
        assert len(cursor_rows) == 1, "cursor data should still exist on first row"
        # …but CSS rules suppress the visual paint because the list
        # isn't focused. Bridging into the list with Down should
        # surface the cursor visually (focus state flips).
        await pilot.press("down")
        await pilot.pause()
        assert lst.has_focus


@pytest.mark.asyncio
async def test_up_below_top_moves_cursor_only(built_index: Path) -> None:
    """Regression: Up when the cursor is NOT at the topmost row moves
    the cursor up one row instead of jumping to the Input.

    Open Preferences (a sub-screen with several rows), focus the list,
    move down a couple of rows, then press Up — focus stays on the list
    and cursor_index decreases by one selectable row.
    """
    from fnd.tui.menu import SECTION_PREFERENCES
    from fnd.tui.settings_screen import (
        SettingsList,
        SettingsScreen,
        open_settings_section,
    )

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_PREFERENCES)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        # Sub-menu on_mount focuses the list directly.
        lst = screen.query_one(SettingsList)
        assert isinstance(screen.focused, SettingsList)
        # Walk down two selectable rows.
        lst.action_move(1)
        lst.action_move(1)
        await pilot.pause()
        idx_before = lst.cursor_index
        # Up while not at the top: cursor moves, focus stays.
        lst.action_move(-1)
        await pilot.pause()
        assert isinstance(screen.focused, SettingsList)
        assert lst.cursor_index < idx_before

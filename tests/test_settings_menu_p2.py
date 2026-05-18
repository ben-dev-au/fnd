"""Phase 2 — Settings menu UI/UX behaviors.

Covers the redesign concerns the user raised:

  1. Root menu is a *short list of categories*, not a flat dump.
  2. Cursor never lands on a KIND_HEADER row on a sub-screen that has them.
  3. Right-arrow drills the same as Enter; left-arrow pops back.
  4. `:` opens the root menu; second press closes (toggle).
  5. `?` from main app opens the Keybindings sub-screen directly and
     one Esc returns to main.
  6. Esc from a sub-screen pops back to the parent settings screen.
  7. Activating a key row dispatches the action AND closes the settings
     stack (so the action runs in the main app).
  8. Right-arrow is drill-only; on a scalar row it must NOT open the
     edit bar.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.tui import FNDApp


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_root_menu_is_short_list_of_categories(built_index: Path) -> None:
    """`:` opens a small list of category drill-ins — Preferences,
    Collections, Keybindings, Open config file, Open keybindings file.
    No content piled on top of each other."""
    from fnd.tui.menu import KIND_EXTERNAL
    from fnd.tui.settings_screen import SettingsList, SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        assert screen._breadcrumb == ()
        lst = screen.query_one(SettingsList)
        # Root has exactly the five expected drill / action rows.
        labels = [it.label for it in lst._items]
        assert labels == [
            "Preferences",
            "Collections",
            "Keybindings",
            "Open config file in editor",
            "Open keybindings file in editor",
        ]
        # Every row is "external" (push a sub-screen / run an action).
        assert all(it.kind == KIND_EXTERNAL for it in lst._items)
        # Cursor lands on the first row (Preferences).
        assert lst._items[lst.cursor_index].label == "Preferences"


@pytest.mark.asyncio
async def test_cursor_skips_headers_on_keybindings(built_index: Path) -> None:
    """The Keybindings sub-screen has KIND_HEADER group separators
    (Global, Results pane, etc.). Cursor must skip them."""
    from fnd.tui.menu import KIND_HEADER, SECTION_KEYBINDINGS
    from fnd.tui.settings_screen import (
        SettingsList,
        SettingsScreen,
        open_settings_section,
    )

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_KEYBINDINGS)
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        lst = app.screen.query_one(SettingsList)
        # Sanity: there ARE headers on this sub-screen.
        assert any(it.kind == KIND_HEADER for it in lst._items)
        for _ in range(80):
            lst.action_move(1)
            assert lst._items[lst.cursor_index].kind != KIND_HEADER
        for _ in range(160):
            lst.action_move(-1)
            assert lst._items[lst.cursor_index].kind != KIND_HEADER


@pytest.mark.asyncio
async def test_left_arrow_pops(built_index: Path) -> None:
    """`←` is Esc (pop) — back-stack navigation."""
    from fnd.tui.settings_screen import SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        await pilot.press("left")
        await pilot.pause()
        assert not isinstance(app.screen, SettingsScreen)


@pytest.mark.asyncio
async def test_right_arrow_only_drills(built_index: Path) -> None:
    """`→` is navigation parity for drilling sub-screens only. On a
    scalar/toggle/action row it must NOT activate (no edit-bar opens,
    no toggle flips, no action runs). Enter is the activate key."""
    from fnd.tui.menu import KIND_SCALAR, SECTION_PREFERENCES
    from fnd.tui.settings_screen import (
        EditBar,
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
        lst = screen.query_one(SettingsList)
        scalar_idx = next(
            i
            for i, item in enumerate(lst._items)
            if item.kind == KIND_SCALAR and item.id == "pref.result_limit"
        )
        lst.cursor_index = scalar_idx
        await pilot.press("right")
        await pilot.pause()
        bar = screen.query_one(EditBar)
        assert "-hidden" in bar.classes, "right arrow should not open edit bar on a scalar"
        await pilot.press("enter")
        await pilot.pause()
        assert "-hidden" not in bar.classes, "enter should open edit bar on a scalar"


@pytest.mark.asyncio
async def test_palette_toggle(built_index: Path) -> None:
    """Pressing `:` again closes an already-open settings menu."""
    from fnd.tui.settings_screen import SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        app.action_open_command_palette()
        await pilot.pause()
        assert not isinstance(app.screen, SettingsScreen)


@pytest.mark.asyncio
async def test_help_pushes_keybindings_subscreen(built_index: Path) -> None:
    """`?` from the main app pushes the Keybindings sub-screen directly
    (single push), so one Esc returns to the main app."""
    from fnd.tui.settings_screen import SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_help()
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        assert app.screen._breadcrumb == ("Keybindings",)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, SettingsScreen)


@pytest.mark.asyncio
async def test_drilling_into_preferences_then_esc_returns_to_root(built_index: Path) -> None:
    """Drilling from root into a category pushes a new sub-screen; Esc
    pops back to the root, not all the way to the main app."""
    from fnd.tui.settings_screen import SettingsList, SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        root_screen = app.screen
        assert isinstance(root_screen, SettingsScreen)
        # Drill into Preferences.
        lst = root_screen.query_one(SettingsList)
        prefs = next(item for item in lst._items if item.id == "root.preferences")
        root_screen._activate_item(prefs)
        await pilot.pause()
        sub_screen = app.screen
        assert isinstance(sub_screen, SettingsScreen)
        assert sub_screen is not root_screen
        assert sub_screen._breadcrumb == ("Preferences",)
        # Esc pops the sub-screen → back to root.
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is root_screen
        # Another Esc closes the menu entirely.
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, SettingsScreen)


@pytest.mark.asyncio
async def test_activating_key_action_closes_menu_and_runs(built_index: Path) -> None:
    """Keys & Actions rows are launcher rows: Enter dispatches the
    action AND closes the menu so the user lands back in the main app."""
    from fnd.tui.menu import KIND_ACTION
    from fnd.tui.settings_screen import SettingsList, SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_help()  # pushes Keybindings sub-screen
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        lst = screen.query_one(SettingsList)
        target = next(
            (
                item
                for item in lst._items
                if item.kind == KIND_ACTION and item.action_id == "focus_query"
            ),
            None,
        )
        assert target is not None
        screen._activate_item(target)
        await pilot.pause()
        assert not isinstance(app.screen, SettingsScreen)
        from textual.widgets import Input

        focused = app.focused
        assert isinstance(focused, Input)
        assert focused.id == "query_bar"


@pytest.mark.asyncio
async def test_root_has_open_config_file_row(built_index: Path) -> None:
    """Configuration → Open config file is reachable directly from the
    root menu (no nesting required for a one-off action)."""
    from fnd.tui.settings_screen import SettingsList, SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        lst = screen.query_one(SettingsList)
        labels = [item.label for item in lst._items]
        assert "Open config file in editor" in labels

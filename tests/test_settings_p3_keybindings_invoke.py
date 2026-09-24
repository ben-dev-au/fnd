"""Press-key-to-invoke on Keybindings + drill cue mode."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.tui import FNDApp
from tests._pilot_wait import settings_ready


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_pressing_key_in_keybindings_invokes_action(built_index: Path) -> None:
    """Spec: Keybindings › Press-key-to-invoke — pressing a listed key
    dispatches the action and closes the settings stack."""
    from fnd.tui.settings_screen import SettingsList, SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_help()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        # Focus the list (not the search input).
        screen.query_one(SettingsList).focus()
        # Press `o`: runs action_focus_outline_panel and closes the menu.
        await pilot.press("o")
        await pilot.pause()
        assert not isinstance(app.screen, SettingsScreen)


@pytest.mark.asyncio
async def test_pressing_key_while_search_focused_does_not_invoke(built_index: Path) -> None:
    """Spec: Press-key-to-invoke applies only when the LIST has focus;
    typing in the search filter must not trigger actions."""
    from textual.widgets import Input

    from fnd.tui.settings_screen import SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_help()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        screen.query_one("#settings_search", Input).focus()
        await pilot.press("o")
        await pilot.pause()
        # Search has 'o' in it; menu still up.
        assert isinstance(app.screen, SettingsScreen)


def test_drill_summary_mode_default_and_validation() -> None:
    """Spec: Drill-cue preference — defaults to always_show; validates set."""
    from pydantic import ValidationError

    from fnd.config import Defaults

    d = Defaults()
    assert d.drill_summary_mode == "always_show"
    # Each known mode round-trips.
    for mode in ("always_show", "smart", "always_ellipsis"):
        Defaults(drill_summary_mode=mode)
    # Unknown values rejected.
    try:
        Defaults(drill_summary_mode="banana")  # type: ignore[arg-type]
    except ValidationError:
        return
    raise AssertionError("expected ValidationError for unknown mode")


@pytest.mark.asyncio
async def test_drill_mode_always_ellipsis(
    built_index: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: Drill-cue preference — `always_ellipsis` mode renders `…`
    instead of content summaries."""
    from fnd.config import write_setting

    # Isolate config reads/writes to the tmp dir.
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)

    # Write directly to cfg_path so the patched default_config_path() picks it up.
    write_setting(
        config_path=cfg_path,
        dotted_path="defaults.drill_summary_mode",
        value="always_ellipsis",
    )

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await settings_ready(pilot, app)
        screen = app.screen
        from fnd.tui.settings_screen import SettingsList, SettingsScreen

        assert isinstance(screen, SettingsScreen)
        lst = screen.query_one(SettingsList)
        preferences = next(it for it in lst._items if it.label == "Preferences")
        # In always_ellipsis mode the trailing value is `…`.
        assert preferences.trailing_value(app) == "…"


@pytest.mark.asyncio
@pytest.mark.parametrize("chord", ["alt+o", "ctrl+o"])
async def test_every_chord_a_row_lists_invokes_it(built_index: Path, chord: str) -> None:
    """A row listing several chords ("⌥+O / Ctrl+O") runs on any of them."""
    from fnd.tui.settings_screen import SettingsList, SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_help()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        screen.query_one(SettingsList).focus()
        invoked: list[str] = []
        app.action_open_at_locator = lambda: invoked.append(chord)  # type: ignore[method-assign]
        await pilot.press(chord)
        await pilot.pause()
        assert not isinstance(app.screen, SettingsScreen)
        assert invoked == [chord]


@pytest.mark.asyncio
async def test_right_moves_around_the_sheet_and_invokes_nothing(built_index: Path) -> None:
    """Right is bound in the app (expand), but on the sheet it only moves."""
    from fnd.tui.settings_screen import SettingsList, SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_help()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        screen.query_one(SettingsList).focus()
        await pilot.press("right")
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)


@pytest.mark.asyncio
async def test_a_sheet_key_the_user_rebound_still_moves_around_the_sheet(
    built_index: Path,
) -> None:
    """A user binding on `j` runs in the app, but `j` still moves the sheet."""
    from fnd.tui.actions import load_keymap
    from fnd.tui.settings_screen import SettingsList, SettingsScreen

    keymap = load_keymap(built_index / "no-such-keybindings.toml")
    keymap.bindings["j"] = "nav_next_match"
    app = FNDApp(index_dir=built_index, keymap=keymap)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_help()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        sheet = screen.query_one(SettingsList)
        sheet.focus()
        before = sheet.cursor_index
        await pilot.press("j")
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        assert sheet.cursor_index == before + 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chord", "action"),
    [("O", "open_with_menu"), ("R", "reveal_in_file_manager"), ("o", "focus_outline_panel")],
)
async def test_a_capital_runs_its_own_row_not_the_lower_case_one(
    built_index: Path, chord: str, action: str
) -> None:
    """Shift+O runs Open with, not the Outline row that `o` runs."""
    from fnd.tui.settings_screen import SettingsList, SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_help()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        screen.query_one(SettingsList).focus()
        invoked: list[str] = []
        for name in ("open_with_menu", "reveal_in_file_manager", "focus_outline_panel"):
            setattr(app, f"action_{name}", lambda name=name: invoked.append(name))
        await pilot.press(chord)
        await pilot.pause()
        assert invoked == [action]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["x", "Q", "N"])
async def test_a_key_the_app_does_not_bind_runs_nothing(built_index: Path, key: str) -> None:
    """Only what a key runs in the app runs here: never a row of another case."""
    from fnd.tui.settings_screen import SettingsList, SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_help()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        screen.query_one(SettingsList).focus()
        await pilot.press(key)
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)


@pytest.mark.asyncio
async def test_page_down_pages_the_sheet_even_when_the_user_bound_it(built_index: Path) -> None:
    """PageDown moves the sheet; a user binding on it runs only in the app."""
    from fnd.tui.actions import load_keymap
    from fnd.tui.settings_screen import SettingsList, SettingsScreen

    keymap = load_keymap(built_index / "no-such-keybindings.toml")
    keymap.bindings["pagedown"] = "nav_next_match"
    app = FNDApp(index_dir=built_index, keymap=keymap)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_help()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        sheet = screen.query_one(SettingsList)
        sheet.focus()
        before = sheet.cursor_index
        await pilot.press("pagedown")
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        assert sheet.cursor_index > before + 1, "PageDown moved less than a page"

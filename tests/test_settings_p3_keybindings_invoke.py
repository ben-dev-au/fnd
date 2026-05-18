"""Phase 3 — press-key-to-invoke on Keybindings + drill cue mode."""

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
        # Press `o` — should run action_open_at_locator and close menu.
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
        await pilot.pause()
        screen = app.screen
        from fnd.tui.settings_screen import SettingsList, SettingsScreen

        assert isinstance(screen, SettingsScreen)
        lst = screen.query_one(SettingsList)
        preferences = next(it for it in lst._items if it.label == "Preferences")
        # In always_ellipsis mode the trailing value is `…`.
        assert preferences.trailing_value(app) == "…"

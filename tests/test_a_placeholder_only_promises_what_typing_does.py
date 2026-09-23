"""A filter box only promises "Type to filter…" where typing reaches it.

On the Keybindings sheet the LIST has focus so press-key-to-invoke works, which
is the design, and the sheet lists `q Quit` three rows under that box: typing
to narrow the list quits the app. The box names the key that reaches it, as the
filter browser's does.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Input

from fnd.tui import FNDApp
from fnd.tui.settings_screen import SettingsList, SettingsScreen


@pytest.mark.asyncio
async def test_the_keybindings_sheet_opens_on_the_list_not_the_box(tmp_index_dir: Path) -> None:
    """The premise. If this ever changes, the placeholder can change back."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        app.action_show_help()
        for _ in range(20):
            await pilot.pause()
        focused = type(app.focused).__name__

    assert focused == "SettingsList"


@pytest.mark.asyncio
async def test_the_box_names_the_key_that_reaches_it(tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        app.action_show_help()
        for _ in range(20):
            await pilot.pause()
        placeholder = app.screen.query_one("#settings_search", Input).placeholder

    assert "/" in placeholder, placeholder
    assert "Type to filter" not in placeholder, "it instructed the one thing that does not work"


@pytest.mark.asyncio
async def test_slash_still_reaches_it_and_filtering_works(tmp_index_dir: Path) -> None:
    """The control: the key the box names must do what it says."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        app.action_show_help()
        for _ in range(20):
            await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        before = len(screen.query_one(SettingsList)._items)

        await pilot.press("slash")
        await pilot.pause()
        focused_after_slash = type(app.focused).__name__
        for ch in "quit":
            await pilot.press(ch)
        for _ in range(6):
            await pilot.pause()
        after = len(screen.query_one(SettingsList)._items)
        still_running = app.is_running

    assert focused_after_slash == "Input", "/ must reach the box"
    assert still_running, "the letters must reach the box, not the bindings"
    assert after < before, (before, after)


@pytest.mark.asyncio
async def test_a_documented_key_that_belongs_elsewhere_does_not_close_the_sheet(
    tmp_index_dir: Path,
) -> None:
    """Rows for another screen's widget keys carry no action, so invoking one
    closed the whole settings stack and ran nothing."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        app.action_show_help()
        for _ in range(20):
            await pilot.pause()
        # `Ctrl+D` is listed under Source form; it belongs to a screen that is
        # not open, and the row carries no action id.
        await pilot.press("ctrl+d")
        for _ in range(6):
            await pilot.pause()
        still_there = isinstance(app.screen, SettingsScreen)

    assert still_there, "a row with nothing to run closed the sheet"


@pytest.mark.asyncio
async def test_a_real_action_row_still_invokes(tmp_index_dir: Path) -> None:
    """The control: press-key-to-invoke is the feature, and it must survive."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        app.action_show_help()
        for _ in range(20):
            await pilot.pause()
        await pilot.press("h")  # Highlights: a registry action with a key
        for _ in range(6):
            await pilot.pause()
        left = not isinstance(app.screen, SettingsScreen)

    assert left, "the sheet must still dispatch a listed action"

"""Choosing Discard on the unsaved-changes guard drops the unsaved editor.

Both discard routes only ever popped SettingsScreens, and no editor that can hold
unsaved work is one: "Discard and open the menu" opened the menu over the
still-dirty editor, and "Discard and close" closed nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import OptionList, Static

from fnd.tui import FNDApp
from fnd.tui.settings_screen import SettingsScreen, UnsavedChangesScreen, open_settings


class _Editor(Screen[None]):
    def compose(self) -> ComposeResult:
        yield Static("editor")

    def unsaved_work(self) -> tuple[str, Callable[[], None]] | None:
        return "This form", lambda: None


async def _pause(pilot: Any, n: int = 10) -> None:
    for _ in range(n):
        await pilot.pause()


async def _discard(app: FNDApp, pilot: Any) -> None:
    assert app.screen.__class__ is UnsavedChangesScreen, "the premise"
    options = app.screen.query_one("#confirm_list", OptionList)
    options.highlighted = next(i for i, o in enumerate(options._options) if o.id == "discard")
    await pilot.pause()
    options.action_select()
    await _pause(pilot)


def _stack(app: FNDApp) -> list[str]:
    return [type(s).__name__ for s in app.screen_stack]


@pytest.mark.asyncio
async def test_discard_and_open_the_menu_drops_the_editor(tmp_index_dir: Path) -> None:
    """The menu opens fresh, with the discarded editor gone from beneath it."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        open_settings(app)
        await _pause(pilot)
        app.push_screen(_Editor())
        await _pause(pilot, 6)
        app.action_open_command_palette()
        await _pause(pilot, 8)
        await _discard(app, pilot)
        stack = _stack(app)

    assert "_Editor" not in stack, stack
    assert stack[-1] == SettingsScreen.__name__, stack
    assert stack.count(SettingsScreen.__name__) == 1, stack


@pytest.mark.asyncio
async def test_discard_and_close_drops_the_editor_and_the_stack(tmp_index_dir: Path) -> None:
    """Back at the app, with nothing of the settings stack left."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        open_settings(app)
        await _pause(pilot)
        app.push_screen(_Editor())
        await _pause(pilot, 6)
        app._close_settings_stack()
        await _pause(pilot, 8)
        await _discard(app, pilot)
        stack = _stack(app)

    assert "_Editor" not in stack, stack
    assert SettingsScreen.__name__ not in stack, stack

"""Typing a word into a numeric setting surfaced Python's own ValueError.

`Result limit · 1-1000   Notes   invalid: invalid literal for int() with base
10: 'Notes'`: the row already knows the range it wants, and the message named
the coercion function instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input, Static

from fnd.tui import FNDApp
from fnd.tui.menu import SECTION_PREFERENCES
from fnd.tui.settings_screen import (
    EditBar,
    SettingsList,
    SettingsScreen,
    open_settings_section,
)
from tests._pilot_wait import settings_ready


async def _reject(app: FNDApp, pilot: Any, row_id: str, typed: str) -> str:
    open_settings_section(app, SECTION_PREFERENCES)
    await settings_ready(pilot, app)
    screen = app.screen
    assert isinstance(screen, SettingsScreen)
    lst = screen.query_one(SettingsList)
    idx = next(i for i, it in enumerate(lst._items) if it.id == row_id)
    lst.cursor_index = idx
    screen._activate_item(lst._items[idx])
    await pilot.pause()
    bar = screen.query_one(EditBar)
    bar.query_one("#editor_input", Input).value = typed
    await pilot.press("enter")
    await pilot.pause()
    return str(bar.query_one(".-edit-error", Static).render())


@pytest.mark.asyncio
async def test_a_word_in_a_number_field_is_not_told_about_int(tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        message = await _reject(app, pilot, "pref.result_limit", "Notes")

    assert "invalid literal for int()" not in message, message
    assert "1-1000" in message, message


@pytest.mark.asyncio
async def test_a_valid_number_is_still_accepted(tmp_index_dir: Path) -> None:
    """The control: the guard must not reject what the field is for."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        message = await _reject(app, pilot, "pref.result_limit", "300")

    assert not message.strip(), message


@pytest.mark.asyncio
def test_a_non_numeric_row_keeps_its_own_message() -> None:
    """The control on scope: only int/float rows get the reworded message.

    Asserting only that an int row exists passes with `_coercion_error`
    deleted, so a non-numeric coercion goes through the same call.
    """
    from fnd.tui.menu import MenuItem
    from fnd.tui.settings_screen import _coercion_error

    def _picky(_raw: str) -> object:
        raise ValueError("that is not a colour")

    row = MenuItem(id="probe", label="Accent", hint="a colour name", coerce=_picky)
    said = _coercion_error(_picky, row.hint, ValueError("that is not a colour"))

    assert "that is not a colour" in said, said
    assert "whole number" not in said, said


def test_a_numeric_row_does_not_keep_pythons_message() -> None:
    """The other half, so the pair discriminates in both directions."""
    from fnd.tui.settings_screen import _coercion_error

    said = _coercion_error(int, "1-1000", ValueError("invalid literal for int() with base 10"))

    assert "invalid literal" not in said, said
    assert "1-1000" in said, said

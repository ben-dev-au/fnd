"""A picker opens with its cursor on the current value, not the first option.

A single-select picker commits on Enter, so a cursor parked on option 0 turned
"open it to look, press Enter" into a change of setting.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.widgets import OptionList

from fnd.tui import FNDApp
from fnd.tui.menu import KIND_PICKER, ChoiceOption, MenuItem
from fnd.tui.settings_screen import PickerScreen


def _item(current: Any, sink: list[Any], *, multi: bool = False) -> MenuItem:
    return MenuItem(
        id="probe",
        label="Probe",
        kind=KIND_PICKER,
        multi=multi,
        choices_provider=lambda _app: [
            ChoiceOption(value="a", label="A"),
            ChoiceOption(value="b", label="B"),
            ChoiceOption(value="c", label="C"),
        ],
        picker_getter=lambda _app: current,
        picker_setter=lambda _app, v: sink.append(v),
    )


async def _open(app: FNDApp, pilot: Any, item: MenuItem) -> OptionList:
    await pilot.pause()
    app.push_screen(PickerScreen(item))
    for _ in range(12):
        await pilot.pause()
    return app.screen.query_one("#picker_list", OptionList)


@pytest.mark.asyncio
async def test_opening_a_picker_and_pressing_enter_keeps_the_value(tmp_index_dir: Path) -> None:
    """Enter on a freshly opened single-select picker re-selects what was set."""
    sink: list[Any] = []
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test() as pilot:
        options = await _open(app, pilot, _item("c", sink))
        assert options.highlighted == 2
        await pilot.press("enter")
        for _ in range(8):
            await pilot.pause()
    assert sink == ["c"], sink


@pytest.mark.asyncio
async def test_a_multi_picker_opens_on_its_first_ticked_value(tmp_index_dir: Path) -> None:
    """The cursor starts where the selection is, not above it."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test() as pilot:
        options = await _open(app, pilot, _item(["b", "c"], [], multi=True))
        assert options.highlighted == 1

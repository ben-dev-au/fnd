"""Every numeric preference printed a range twice and enforced none of it.

Seven of seven rows accepted and persisted out-of-range values silently:
`result_limit = 99999` against `1-1000`, `preview_warm_margin = -3` against
`0-20`. The validator demonstrably knows the range: it quotes it when refusing
letters, then waves 99999 through.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input, Static

from fnd.tui import FNDApp
from fnd.tui.menu import KIND_SCALAR, SECTION_PREFERENCES
from fnd.tui.settings_screen import EditBar, SettingsList, SettingsScreen, open_settings_section
from tests._pilot_wait import settings_ready

_RANGE = re.compile(r"^([\d.]+)-([\d.]+)$")


async def _submit(app: FNDApp, pilot: Any, row_id: str, typed: str) -> str:
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
@pytest.mark.parametrize(
    ("row_id", "typed"),
    [
        ("pref.result_limit", "99999"),
        ("pref.preview_chunks", "0"),
        ("pref.preview_warm_margin", "-3"),
    ],
)
async def test_an_out_of_range_value_is_refused(
    row_id: str, typed: str, tmp_index_dir: Path, isolated_config_path: Path
) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        message = await _submit(app, pilot, row_id, typed)

    assert message.strip(), f"{row_id} accepted {typed} without a word"
    written = (
        isolated_config_path.read_text(encoding="utf-8") if isolated_config_path.exists() else ""
    )
    assert typed not in written, f"{row_id} persisted {typed}: {written[:200]}"


@pytest.mark.asyncio
async def test_a_value_inside_the_range_is_still_accepted(
    tmp_index_dir: Path, isolated_config_path: Path
) -> None:
    """The control: the guard must not reject what the row is for."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        message = await _submit(app, pilot, "pref.result_limit", "300")

    assert not message.strip(), message


@pytest.mark.asyncio
async def test_every_stated_range_is_a_declared_one(tmp_index_dir: Path) -> None:
    """The hint and the enforcement are separate fields, so a guard holds them
    together: a row that prints a range must declare the same one."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_PREFERENCES)
        await settings_ready(pilot, app)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        rows = [it for it in screen.query_one(SettingsList)._items if it.kind == KIND_SCALAR]

    mismatched = []
    for item in rows:
        m = _RANGE.match(item.hint or "")
        if not m:
            continue
        want = (float(m.group(1)), float(m.group(2)))
        if getattr(item, "bounds", None) != want:
            mismatched.append((item.id, item.hint, getattr(item, "bounds", None)))
    assert not mismatched, f"rows printing a range they do not declare: {mismatched}"

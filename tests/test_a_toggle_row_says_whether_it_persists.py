"""`Highlights` looks like the five persisted toggles beside it and is not one.

It writes no config key (`highlights_enabled` lives on the SearchController
and is `True` at construction), so it resets on relaunch while its neighbours
survive. Nothing on the row says which kind it is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fnd.tui import FNDApp
from fnd.tui.menu import KIND_TOGGLE, SECTION_PREFERENCES
from fnd.tui.settings_screen import SettingsList, SettingsScreen, open_settings_section
from tests._pilot_wait import settings_ready

#: What a row that does not survive a relaunch has to admit to.
_SESSION_WORDS = ("this session", "not saved", "until you quit", "resets")


async def _toggle_rows(app: FNDApp, pilot: Any) -> list[Any]:
    open_settings_section(app, SECTION_PREFERENCES)
    await settings_ready(pilot, app)
    screen = app.screen
    assert isinstance(screen, SettingsScreen)
    return [it for it in screen.query_one(SettingsList)._items if it.kind == KIND_TOGGLE]


@pytest.mark.asyncio
async def test_every_toggle_either_persists_or_says_it_does_not(
    tmp_index_dir: Path, isolated_config_path: Path
) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        rows = await _toggle_rows(app, pilot)
        assert rows, "no toggle row to speak for"

        silent: list[str] = []
        for item in rows:
            before = (
                isolated_config_path.read_text(encoding="utf-8")
                if isolated_config_path.exists()
                else ""
            )
            current = bool(item.toggle_getter(app)) if item.toggle_getter else False
            if item.toggle_setter is None:
                continue
            item.toggle_setter(app, not current)
            await pilot.pause()
            after = (
                isolated_config_path.read_text(encoding="utf-8")
                if isolated_config_path.exists()
                else ""
            )
            item.toggle_setter(app, current)
            await pilot.pause()
            if after != before:
                continue
            said = (item.description or "").lower()
            if not any(word in said for word in _SESSION_WORDS):
                silent.append(item.id)

    assert not silent, f"toggles that write nothing and do not say so: {silent}"


@pytest.mark.asyncio
async def test_a_persisted_toggle_is_not_made_to_apologise(
    tmp_index_dir: Path, isolated_config_path: Path
) -> None:
    """The control: a row that DOES survive must not claim otherwise."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        rows = await _toggle_rows(app, pilot)
        fuzzy = next(it for it in rows if it.id == "pref.fuzzy_enabled")
        said = (fuzzy.description or "").lower()

    assert not any(word in said for word in _SESSION_WORDS), said

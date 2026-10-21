"""A repaint requested inside a batch update reaches the screen when the batch ends."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from textual.screen import ModalScreen, Screen

from fnd.config import load
from fnd.tui import FNDApp
from fnd.tui.settings_screen import SettingsList


class _SeeThroughModal(ModalScreen[None]):
    DEFAULT_CSS = "_SeeThroughModal { background: $surface 75%; }"


async def _painted_after_a_batch(app: FNDApp, screen: Screen[Any]) -> bool:
    lst = screen.query_one(SettingsList)
    with app.batch_update():
        lst.refresh()
        await asyncio.sleep(0.1)
        assert lst in screen._dirty_widgets
    for _ in range(150):
        await asyncio.sleep(0.02)
        if lst not in screen._dirty_widgets:
            return True
    return False


@pytest.mark.asyncio
async def test_a_repaint_held_by_a_batch_lands_without_further_input(
    tmp_index_dir: Path, isolated_config_path: Path
) -> None:
    """The top screen paints a frame held by a batch once the batch ends."""
    del isolated_config_path
    app = FNDApp(index_dir=tmp_index_dir, config=load())
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        assert await _painted_after_a_batch(app, app.screen)


@pytest.mark.asyncio
async def test_a_screen_behind_a_see_through_modal_paints_after_a_batch(
    tmp_index_dir: Path, isolated_config_path: Path
) -> None:
    """A screen visible behind a translucent modal paints a frame held by a batch."""
    del isolated_config_path
    app = FNDApp(index_dir=tmp_index_dir, config=load())
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        under = app.screen
        await app.push_screen(_SeeThroughModal())
        await pilot.pause()
        assert under in app._background_screens
        assert await _painted_after_a_batch(app, under)

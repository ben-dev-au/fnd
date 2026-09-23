"""`Yes, update all 1 collections` was the label a single collection got."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import OptionList

from fnd.tui import FNDApp
from fnd.tui.settings_screen import UpdateAllConfirm


async def _confirm_label(app: FNDApp, pilot: object, names: list[str]) -> str:
    app.push_screen(UpdateAllConfirm(collection_names=names))
    for _ in range(6):
        await pilot.pause()  # type: ignore[attr-defined]
    opts = app.screen.query_one("#confirm_list", OptionList)
    return str(opts.get_option("yes").prompt)


@pytest.mark.asyncio
async def test_one_collection_is_not_called_1_collections(tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        label = await _confirm_label(app, pilot, ["papers"])

    assert "1 collections" not in label, label


@pytest.mark.asyncio
async def test_several_still_say_how_many(tmp_index_dir: Path) -> None:
    """The control: the count is the point of the label when there is one."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        label = await _confirm_label(app, pilot, ["papers", "notes", "wine"])

    assert "3 collections" in label, label

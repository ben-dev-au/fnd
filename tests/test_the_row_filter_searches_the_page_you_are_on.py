"""The filter box searched everywhere except the page in front of you.

`walk_all_sections` deliberately does not descend per-collection sub-screens,
so on `Collections › Work` typing `Delete coll` answered "No matches" for a row
visible a keystroke earlier. The one screen where a wrong "not found" matters
is the one carrying a destructive action.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.config import CollectionConfig, Config, SourceConfig
from fnd.tui import FNDApp
from fnd.tui.menu import _provider_collection
from fnd.tui.settings_screen import SettingsList, SettingsScreen


async def _filtered(app: FNDApp, pilot: object, query: str) -> list[str]:
    screen = app.screen
    assert isinstance(screen, SettingsScreen)
    screen._apply_filter(query)
    return [it.label for it in screen.query_one(SettingsList)._items]


@pytest.fixture
def app_on_a_collection_page(fixtures_dir: Path, tmp_index_dir: Path) -> FNDApp:
    cfg = Config(collections={"Work": CollectionConfig(sources=[SourceConfig(path=fixtures_dir)])})
    return FNDApp(index_dir=tmp_index_dir, config=cfg)


@pytest.mark.asyncio
async def test_a_visible_row_survives_its_own_filter(app_on_a_collection_page: FNDApp) -> None:
    """The row is on screen; the filter must not deny it exists."""
    app = app_on_a_collection_page
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            SettingsScreen(
                breadcrumb=("Collections", "Work"),
                items=_provider_collection(app, "Work"),
                provider=lambda a: tuple(_provider_collection(a, "Work")),
            )
        )
        await pilot.pause()
        labels = await _filtered(app, pilot, "Delete coll")

    assert any("Delete collection" in label for label in labels), labels


@pytest.mark.asyncio
async def test_the_page_you_are_on_ranks_first(app_on_a_collection_page: FNDApp) -> None:
    """`Rebuild` matches this page's row and a global one; the visible row wins."""
    app = app_on_a_collection_page
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            SettingsScreen(
                breadcrumb=("Collections", "Work"),
                items=_provider_collection(app, "Work"),
                provider=lambda a: tuple(_provider_collection(a, "Work")),
            )
        )
        await pilot.pause()
        labels = await _filtered(app, pilot, "rebuild")

    assert labels, labels
    assert "Rebuild index" in labels[0], labels

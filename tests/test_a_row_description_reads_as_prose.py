"""A settings row carries no RST markup and names only values the picker offers.

The source form's App row read "Leave as '(default)' … See ``[apps]`` in
config.toml": the row displays `(unset)`, the picker offers
`(default: use global resolver)`, and the generated config has `[app_defaults]`
and no `[apps]`. The double backticks render literally on screen.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.tui import FNDApp
from fnd.tui.menu import (
    SECTION_COLLECTIONS,
    SECTION_FILTERS,
    SECTION_INDEXING,
    SECTION_KEYBINDINGS,
    SECTION_PREFERENCES,
)
from fnd.tui.settings_screen import SettingsList, SettingsScreen, open_settings_section
from tests._pilot_wait import settings_ready


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "section",
    [
        SECTION_PREFERENCES,
        SECTION_COLLECTIONS,
        SECTION_FILTERS,
        SECTION_INDEXING,
        SECTION_KEYBINDINGS,
    ],
)
async def test_no_row_description_carries_rst_markup(section: str, tmp_index_dir: Path) -> None:
    """Double backticks render as two backticks on screen, not as code."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        open_settings_section(app, section)
        await settings_ready(pilot, app)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        offenders = [
            it.id for it in screen.query_one(SettingsList)._items if "``" in (it.description or "")
        ]

    assert not offenders, f"rows showing RST markup to the user: {offenders}"


@pytest.mark.asyncio
async def test_the_source_app_row_names_a_section_the_config_has(tmp_index_dir: Path) -> None:
    from fnd.config import Config
    from fnd.config_render import render_config

    generated = render_config(Config())
    from fnd.tui.settings_screen import SourceFormScreen

    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        screen = SourceFormScreen(collection_name="notes", source_index=None)
        app.push_screen(screen)
        for _ in range(15):
            await pilot.pause()
        row = next(it for it in screen.query_one(SettingsList)._items if it.label == "App")
        said = row.description or ""

    assert "``" not in said, said
    named = {tok for tok in said.split() if tok.startswith("[") and tok.endswith("]")}
    missing = [s for s in named if s not in generated]
    assert not missing, f"the row names sections the generated config has not: {missing}"


@pytest.mark.asyncio
async def test_the_default_app_rows_do_not_offer_a_pdf_example_for_every_type(
    tmp_index_dir: Path,
) -> None:
    """One example, written for PDFs, sat on the Markdown and Word rows too."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_PREFERENCES)
        await settings_ready(pilot, app)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        rows = [
            it
            for it in screen.query_one(SettingsList)._items
            if it.id.startswith("pref.app_defaults.") and not it.id.endswith(".pdf")
        ]

    assert rows, "no non-PDF app row to speak for"
    wrong = [it.id for it in rows if "Skim" in (it.description or "")]
    assert not wrong, f"non-PDF rows carrying a PDF example: {wrong}"

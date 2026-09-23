"""An index run finishing keeps a row filter the user was part-way through.

An open settings screen repaints when a run completes, so a stale
`⚠ nothing indexed` cannot outlive the run that cleared it. `refresh_items`
rebuilds the list from the provider, so without re-applying the row filter
four filtered rows become all eight while the box still reads the query.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input

from fnd.tui import FNDApp
from fnd.tui.menu import SECTION_PREFERENCES
from fnd.tui.settings_screen import SettingsList, SettingsScreen, open_settings_section
from tests._pilot_wait import settings_ready


async def _filtered(app: FNDApp, pilot: Any, query: str) -> tuple[SettingsScreen, int]:
    open_settings_section(app, SECTION_PREFERENCES)
    await settings_ready(pilot, app)
    screen = app.screen
    assert isinstance(screen, SettingsScreen)
    box = screen.query_one("#settings_search", Input)
    box.value = query
    for _ in range(6):
        await pilot.pause()
    return screen, len(screen.query_one(SettingsList)._items)


@pytest.mark.asyncio
async def test_a_repaint_keeps_the_rows_the_query_narrowed_to(
    tmp_path: Path, tmp_index_dir: Path, isolated_config_path: Path
) -> None:
    """The repaint has to DO something as well as preserve something.

    Asserting only that the count did not widen is satisfied by
    `refresh_items` returning immediately. The provider is made to report a
    NEW row first, so a repaint that does nothing cannot show it.
    """
    isolated_config_path.write_text(
        f'[[collections.alpha.sources]]\npath = "{tmp_path.as_posix()}"\n', encoding="utf-8"
    )
    from fnd.config import load
    from fnd.tui.menu import SECTION_COLLECTIONS

    app = FNDApp(index_dir=tmp_index_dir, config=load())
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        app._config = load()
        open_settings_section(app, SECTION_COLLECTIONS)
        await settings_ready(pilot, app)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        box = screen.query_one("#settings_search", Input)
        box.value = "alpha"
        for _ in range(6):
            await pilot.pause()
        narrowed = len(screen.query_one(SettingsList)._items)
        assert narrowed > 0, "precondition: the query matches something"
        total = len(screen._items)

        isolated_config_path.write_text(
            f'[[collections.alpha.sources]]\npath = "{tmp_path.as_posix()}"\n'
            f'\n[[collections.alphabet.sources]]\npath = "{tmp_path.as_posix()}"\n',
            encoding="utf-8",
        )
        app._config = load()
        screen.refresh_items()
        for _ in range(6):
            await pilot.pause()
        after = len(screen.query_one(SettingsList)._items)
        labels = " ".join(it.label for it in screen.query_one(SettingsList)._items)
        still_typed = screen.query_one("#settings_search", Input).value

    assert "alphabet" in labels, f"the repaint did not pick up the new row: {labels}"
    assert still_typed == "alpha", "the box kept the query"
    assert after < len(screen._items), f"the repaint widened to the whole list ({after})"
    assert after == narrowed + 1, (narrowed, after, total)


@pytest.mark.asyncio
async def test_a_repaint_keeps_the_search_rendering(tmp_index_dir: Path) -> None:
    """Row COUNT surviving is not the contract: the rendering has to survive.

    Search results are drawn flat with a breadcrumb per row; `set_items` decides
    that from its `breadcrumbs` argument. Passing rows without them redrew the
    subsection borders, which the code says "would fragment the result list",
    and gave the rows their trailing values back.
    """

    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        screen, _ = await _filtered(app, pilot, "texture")
        lst = screen.query_one(SettingsList)
        boxed_before = len(lst.query("#settings_list_body > Vertical"))
        crumbs_before = len(lst._search_breadcrumbs)

        screen.refresh_items()
        for _ in range(6):
            await pilot.pause()
        boxed_after = len(lst.query("#settings_list_body > Vertical"))
        crumbs_after = len(lst._search_breadcrumbs)

    assert crumbs_after == crumbs_before, (crumbs_before, crumbs_after)
    assert boxed_after == boxed_before, (
        f"the repaint drew {boxed_after} subsection boxes where the search had {boxed_before}"
    )


@pytest.mark.asyncio
async def test_an_unfiltered_screen_still_gets_the_new_rows(tmp_index_dir: Path) -> None:
    """The control: the repaint exists to pick up a changed set of rows."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_PREFERENCES)
        await settings_ready(pilot, app)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        before = len(screen.query_one(SettingsList)._items)
        screen.refresh_items()
        for _ in range(6):
            await pilot.pause()
        after = len(screen.query_one(SettingsList)._items)

    assert after == before, (before, after)


@pytest.mark.asyncio
async def test_a_repaint_keeps_the_no_matches_placeholder(tmp_index_dir: Path) -> None:
    """A repaint keeps the "No matches" placeholder, not a blank panel.

    `_on_search_changed` substitutes a "No matches" row when nothing matches;
    the repaint's own copy of the filter must too, or it paints nothing at all
    with the query still in the box.
    """
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        screen, before = await _filtered(app, pilot, "zzzzq")
        assert before == 1, "precondition: the placeholder is the only row"

        screen.refresh_items()
        for _ in range(6):
            await pilot.pause()
        rows = ["".join(s.text for s in strip) for strip in screen._compositor.render_strips()]

    assert any("No matches" in r for r in rows), rows[:12]

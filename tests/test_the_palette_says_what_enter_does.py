"""The palette's footer promised `⏎  Open first`, and `⏎` opens nothing.

Enter on the filter box moves focus into the row list and stops, deliberately:
the handler says why ("no silent toggles, no accidental side-effects"). The
behaviour is right; the label was not, on the first screen of every settings
journey.

The placeholder has the mirror problem. It advertises `(/)`, and it is visible
in exactly the state where `/` is wrong, because the box opens with focus and
takes the slash as text.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Input

from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.settings_screen import SettingsScreen
from tests._pilot_wait import settings_ready


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


async def _palette(pilot: Pilot[None], app: FNDApp) -> SettingsScreen:
    app.action_open_command_palette()
    await settings_ready(pilot, app)
    screen = app.screen
    assert isinstance(screen, SettingsScreen), type(screen).__name__
    return screen


@pytest.mark.asyncio
async def test_the_footer_does_not_promise_to_open(built_index: Path) -> None:
    from textual.widgets import Static

    app = FNDApp(index_dir=built_index)
    async with app.run_test(size=(110, 30)) as pilot:
        screen = await _palette(pilot, app)
        search = screen.query_one("#settings_search", Input)
        search.focus()
        await pilot.pause()
        assert search.has_focus, "the state under test is the one the palette opens in"
        footer = screen.query_one("#footer_hints", Static).render_line(0).text

    assert "Open" not in footer, footer
    assert "first" in footer.lower(), footer


@pytest.mark.asyncio
async def test_enter_on_the_filter_box_opens_nothing(built_index: Path) -> None:
    """The control on the wording: what Enter does is what the label must say."""
    from fnd.tui.settings_screen import SettingsList

    app = FNDApp(index_dir=built_index)
    async with app.run_test(size=(110, 30)) as pilot:
        screen = await _palette(pilot, app)
        search = screen.query_one("#settings_search", Input)
        search.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        same_screen = app.screen is screen
        lst = screen.query_one(SettingsList)
        in_list = lst.has_focus

    assert same_screen, "Enter pushed a screen; the footer's promise would be true"
    assert in_list, "Enter did not hand focus to the list either"


@pytest.mark.asyncio
async def test_the_placeholder_offers_the_slash_only_where_it_works(
    built_index: Path,
) -> None:
    app = FNDApp(index_dir=built_index)
    async with app.run_test(size=(110, 30)) as pilot:
        screen = await _palette(pilot, app)
        search = screen.query_one("#settings_search", Input)
        search.focus()
        await pilot.pause()
        while_focused = str(search.placeholder)

        from fnd.tui.settings_screen import SettingsList

        screen.query_one(SettingsList).focus()
        await pilot.pause()
        while_in_list = str(search.placeholder)

    assert "/" not in while_focused, while_focused
    assert "Filter rows" in while_focused, while_focused
    assert "(/)" in while_in_list, while_in_list

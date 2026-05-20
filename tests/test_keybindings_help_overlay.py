"""End-to-end Pilot coverage for the ``?`` cheat-sheet overlay.

Items (6)-(8) from the app-routing branch fix queue:

* (6) ``?`` pushes Keybindings ON TOP of the current screen, ``?`` again
  or ``Esc`` pops back to that same screen — not the main app.
* (6) The hint resolved from the calling screen surfaces the matching
  section right after Global.
* (7) The inner list body is a ``VerticalScroll`` so 30+ rows scroll.
* (8) The hint section gets ``-hint-section`` on its header AND every
  body row beneath it until the next header.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Static

from fnd.config import CollectionConfig, Config, SourceConfig
from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.settings_screen import (
    SettingsList,
    SettingsScreen,
    SourceFormScreen,
)


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


def _seed_cfg(fixtures_dir: Path) -> Config:
    return Config(
        collections={"default": CollectionConfig(sources=[SourceConfig(path=fixtures_dir)])}
    )


@pytest.mark.asyncio
async def test_question_mark_from_main_app_pushes_keybindings(
    built_index: Path, fixtures_dir: Path
) -> None:
    app = FNDApp(index_dir=built_index, config=_seed_cfg(fixtures_dir))
    async with app.run_test() as pilot:
        await pilot.pause()
        prev = app.screen
        app.action_show_help()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        assert screen._breadcrumb == ("Keybindings",)
        # Re-pressing ? toggles it back off to the prior screen.
        app.action_show_help()
        await pilot.pause()
        assert app.screen is prev


@pytest.mark.asyncio
async def test_question_mark_from_source_form_returns_to_source_form(
    built_index: Path, fixtures_dir: Path
) -> None:
    """The whole point of the fix: ``?`` must not drop the SourceFormScreen
    when toggling off — the user is in the middle of editing a source."""
    app = FNDApp(index_dir=built_index, config=_seed_cfg(fixtures_dir))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(SourceFormScreen(collection_name="default", source_index=0))
        await pilot.pause()
        form = app.screen
        assert isinstance(form, SourceFormScreen)

        # ? from inside the form pushes Keybindings ON TOP.
        app.action_show_help()
        await pilot.pause()
        kb = app.screen
        assert isinstance(kb, SettingsScreen)
        assert kb._breadcrumb == ("Keybindings",)

        # Section relevant to the caller is lifted right after Global.
        lst = kb.query_one(SettingsList)
        header_labels = [
            it.label for it in lst._items if it.kind == "header" and it.header_level == 2
        ]
        assert header_labels[0] == "Global"
        assert header_labels[1] == "Source form", header_labels

        # Esc pops the cheat sheet back to the SourceFormScreen itself,
        # not the main app.
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is form


@pytest.mark.asyncio
async def test_question_mark_toggle_from_sub_screen(built_index: Path, fixtures_dir: Path) -> None:
    """Pressing ? twice from a sub-screen: open, close, still on sub-screen."""
    app = FNDApp(index_dir=built_index, config=_seed_cfg(fixtures_dir))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(SourceFormScreen(collection_name="default", source_index=0))
        await pilot.pause()
        form = app.screen
        app.action_show_help()
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        app.action_show_help()
        await pilot.pause()
        assert app.screen is form


@pytest.mark.asyncio
async def test_settings_list_body_is_vertical_scroll(built_index: Path, fixtures_dir: Path) -> None:
    """(7) The mounted inner container must be ``VerticalScroll`` so
    the existing ``_scroll_cursor_into_view`` has a scrollable parent
    to act on and long lists get a real scrollbar."""
    app = FNDApp(index_dir=built_index, config=_seed_cfg(fixtures_dir))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_help()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        lst = screen.query_one(SettingsList)
        body = lst.query_one("#settings_list_body")
        assert isinstance(body, VerticalScroll), type(body).__name__


@pytest.mark.asyncio
async def test_hint_section_class_lands_on_header_and_body_rows(
    built_index: Path, fixtures_dir: Path
) -> None:
    """(8) The header for the resolved hint section and every body row
    under it (until the next header) carry the ``-hint-section`` CSS
    class — that's the hook the CSS rule paints from."""
    app = FNDApp(index_dir=built_index, config=_seed_cfg(fixtures_dir))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(SourceFormScreen(collection_name="default", source_index=0))
        await pilot.pause()
        app.action_show_help()
        await pilot.pause()
        kb = app.screen
        assert isinstance(kb, SettingsScreen)
        lst = kb.query_one(SettingsList)

        # Walk items + rendered rows together. Every row from the
        # "Source form" header up to (but not including) the next
        # header must carry the marker; rows in other sections must not.
        body = lst.query_one("#settings_list_body", VerticalScroll)
        rows = list(body.query(Static))
        assert len(rows) == len(lst._items)

        in_hint = False
        saw_hint_header = False
        saw_hint_body = False
        for item, row in zip(lst._items, rows, strict=True):
            if item.kind == "header":
                in_hint = item.label == "Source form"
                if in_hint:
                    saw_hint_header = True
                    assert "-hint-section" in row.classes, (
                        item.label,
                        row.classes,
                    )
                else:
                    assert "-hint-section" not in row.classes, (
                        item.label,
                        row.classes,
                    )
                continue
            if in_hint:
                saw_hint_body = True
                assert "-hint-section" in row.classes, (item.label, row.classes)
            else:
                assert "-hint-section" not in row.classes, (
                    item.label,
                    row.classes,
                )
        assert saw_hint_header
        assert saw_hint_body


@pytest.mark.asyncio
async def test_no_hint_means_no_hint_section_class_anywhere(
    built_index: Path, fixtures_dir: Path
) -> None:
    """When ``?`` is pressed without a recognised calling screen there
    is no hint section, so no row carries the marker."""
    app = FNDApp(index_dir=built_index, config=_seed_cfg(fixtures_dir))
    async with app.run_test() as pilot:
        await pilot.pause()
        # Main app, default focus — _keybindings_context_hint returns None.
        # Drop focus explicitly so no pane wins.
        try:
            app.set_focus(None)
        except Exception:
            pass
        await pilot.pause()
        app.action_show_help()
        await pilot.pause()
        kb = app.screen
        assert isinstance(kb, SettingsScreen)
        lst = kb.query_one(SettingsList)
        body = lst.query_one("#settings_list_body", VerticalScroll)
        rows = list(body.query(Static))
        assert not any("-hint-section" in r.classes for r in rows)

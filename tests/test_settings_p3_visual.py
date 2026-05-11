"""Phase 3 (Settings UX redesign) — visual foundation tests."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_indexer_filetypes_exposed_and_complete() -> None:
    """Spec: Add Collection wizard › Includes — file types come from a
    single source of truth, not hardcoded in two places."""
    from acorn.config import INDEXER_FILETYPES

    # Map of extension -> human label. Order is the order the picker shows.
    assert tuple(INDEXER_FILETYPES) == ("md", "pdf", "docx", "pptx", "txt")
    assert INDEXER_FILETYPES["md"] == "Markdown (.md)"
    assert INDEXER_FILETYPES["pdf"] == "PDF (.pdf)"


def test_f3_no_longer_in_keymap() -> None:
    """Spec: Locked decisions — F3 dropped."""
    from acorn.tui.actions import load_keymap

    keymap = load_keymap()
    assert (
        "f3" not in keymap.bindings
    ), f"F3 should not be bound; keymap.bindings has: {keymap.bindings.get('f3')!r}"


def test_detail_strip_renders_description_and_metadata() -> None:
    """Spec: Visual system › Detail strip — 2 lines, description then
    metadata in $text-muted."""
    from acorn.tui.widgets.detail_strip import DetailStrip

    strip = DetailStrip()
    strip._description = "Result limit (1–1000) — max results returned per query."
    strip._metadata = "Stored in defaults.result_limit · Applies on next search"
    rendered = strip._render_lines()
    assert len(rendered) == 2
    assert "Result limit" in str(rendered[0])
    assert "Stored in defaults.result_limit" in str(rendered[1])


def test_row_with_key_renders_bracketed_accent() -> None:
    """Spec: Visual system › Key style — bracketed `[o]` accent."""
    from acorn.tui.menu import KIND_ACTION, MenuItem
    from acorn.tui.settings_screen import _render_row

    item = MenuItem(
        id="k.test",
        label="Open at locator",
        kind=KIND_ACTION,
        key="o",
        action_id="open_at_locator",
    )
    rendered = _render_row(item, app=None, width=80)
    text_str = str(rendered)
    assert "[o]" in text_str, f"expected '[o]' in rendered row; got: {text_str!r}"
    assert "▶" not in text_str


def test_root_container_hugs_content() -> None:
    """Spec: Visual system › Container — height: auto, not 1fr."""
    from acorn.tui.settings_screen import SettingsScreen

    css = SettingsScreen.CSS
    # Find the #settings_box rule and check its height.
    box_rule = css.split("#settings_box {")[1].split("}")[0]
    assert "height: auto" in box_rule
    assert "max-height" in box_rule
    assert "align: center middle" in css  # somewhere in the screen styles


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    from acorn.index import build_index

    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_root_rows_show_trailing_summaries(built_index: Path) -> None:
    """Spec: IA › Root — every drill row shows what's inside."""
    from acorn.tui import AcornApp
    from acorn.tui.settings_screen import SettingsList, SettingsScreen

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        lst = screen.query_one(SettingsList)
        by_label = {it.label: it for it in lst._items}
        preferences = by_label["Preferences"]
        assert preferences.trailing_value(app), "Preferences row needs a trailing summary"
        collections = by_label["Collections"]
        assert "collection" in collections.trailing_value(app).lower()
        keybindings = by_label["Keybindings"]
        assert "key" in keybindings.trailing_value(app).lower()


@pytest.mark.asyncio
async def test_detail_strip_updates_on_cursor_move(built_index: Path) -> None:
    """Spec: Visual system › Detail strip — populates on focus change."""
    from acorn.tui import AcornApp
    from acorn.tui.settings_screen import SettingsList, SettingsScreen
    from acorn.tui.widgets import DetailStrip

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        strip = screen.query_one(DetailStrip)
        # Cursor at index 0 (Preferences). Strip shows Preferences description.
        assert "Preferences" in strip._description or "preferences" in strip._description.lower()
        # Move cursor to Collections.
        lst = screen.query_one(SettingsList)
        lst.action_move(1)
        await pilot.pause()
        assert "Collections" in strip._description or "collection" in strip._description.lower()

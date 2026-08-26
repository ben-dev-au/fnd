"""Phase A — footer hint cluster is per-kind aware.

Whatever Enter does on the focused row, the footer says so. No
"Open" labels when Enter actually toggles or edits.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Static

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp
from tests._pilot_wait import settings_ready


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.fixture
def cfg_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "config.toml"
    p.write_text("")
    monkeypatch.setattr("fnd.config.default_config_path", lambda: p)
    return p


@pytest.fixture
def cfg(cfg_path: Path) -> Config:
    return load(cfg_path)


def _hint_text(app: FNDApp) -> str:
    return str(app.screen.query_one("#footer_hints", Static).content)


@pytest.mark.asyncio
async def test_hint_says_toggle_on_toggle_row(built_index: Path, cfg: Config) -> None:
    from fnd.tui.menu import SECTION_INDEXING
    from fnd.tui.settings_screen import SettingsList, open_settings_section

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_INDEXING)
        await pilot.pause()
        lst = app.screen.query_one(SettingsList)
        for i, it in enumerate(lst._items):
            if it.id == "indexing.auto_resume":
                lst.cursor_index = i
                break
        await pilot.pause()
        text = _hint_text(app)
        assert "Toggle" in text
        assert "Open" not in text


@pytest.mark.asyncio
async def test_hint_says_run_on_action_row(built_index: Path, cfg: Config) -> None:
    """Indexing → cache prune / update / clear rows are KIND_ACTION.
    Footer must reflect that: ⏎ Run, not ⏎ Open."""
    from fnd.tui.menu import SECTION_INDEXING
    from fnd.tui.settings_screen import SettingsList, open_settings_section

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_INDEXING)
        await pilot.pause()
        lst = app.screen.query_one(SettingsList)
        for i, it in enumerate(lst._items):
            if it.id == "pdf_texture.cache_prune":
                lst.cursor_index = i
                break
        await pilot.pause()
        text = _hint_text(app)
        assert "Run" in text
        assert "Open" not in text


@pytest.mark.asyncio
async def test_hint_omits_enter_on_display_row(built_index: Path, cfg: Config) -> None:
    """Display rows have no Enter action. Footer must NOT advertise ⏎."""
    from fnd.tui.menu import SECTION_PDF_TEXTURE
    from fnd.tui.settings_screen import SettingsList, open_settings_section

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_PDF_TEXTURE)
        await pilot.pause()
        lst = app.screen.query_one(SettingsList)
        # Move cursor to a KIND_DISPLAY row.
        for i, it in enumerate(lst._items):
            if it.id == "pdf_texture.cache_size":
                lst.cursor_index = i
                break
        await pilot.pause()
        text = _hint_text(app)
        # The ⏎ glyph must not appear in the hint cluster for display rows.
        assert "⏎" not in text
        assert "Nav" in text
        assert "Back" in text


@pytest.mark.asyncio
async def test_display_row_enter_does_nothing(built_index: Path, cfg: Config) -> None:
    """Pressing Enter on a KIND_DISPLAY row must NOT open the EditBar."""
    from fnd.tui.menu import SECTION_INDEXING
    from fnd.tui.settings_screen import EditBar, SettingsList, open_settings_section

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_INDEXING)
        await pilot.pause()
        lst = app.screen.query_one(SettingsList)
        for i, it in enumerate(lst._items):
            if it.id == "pdf_texture.cache_size":
                lst.cursor_index = i
                break
        lst.action_activate()
        await pilot.pause()
        # EditBar should still be hidden — Enter did not open it.
        bar = app.screen.query_one(EditBar)
        assert "-hidden" in bar.classes


@pytest.mark.asyncio
async def test_hint_says_open_in_editor_on_external_app(built_index: Path, cfg: Config) -> None:
    """External-app rows (config file / keybindings file) reveal in Finder
    via Shift+⏎ and open in $EDITOR via ⏎ — both must appear in the hint."""
    from fnd.tui.settings_screen import SettingsList, SettingsScreen

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await settings_ready(pilot, app)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        lst = screen.query_one(SettingsList)
        # Root screen focuses the search input by default — focus the
        # list so the per-kind hint cluster kicks in instead of the
        # search-input cluster.
        lst.focus()
        await pilot.pause()
        for i, it in enumerate(lst._items):
            if it.id == "root.open_config_file":
                lst.cursor_index = i
                break
        await pilot.pause()
        text = _hint_text(app)
        assert "Open in editor" in text
        assert "Reveal" in text

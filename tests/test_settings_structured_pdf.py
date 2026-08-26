"""Structured PDF section + confirm screen — tests follow
docs/test_patterns/settings_screen.md.

The confirm screen body is state-dependent (install vs uninstall copy).
Step 6b wires the actual install/uninstall worker — these tests only
verify the disclosure + Yes/Cancel pattern.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import OptionList, Static

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


@pytest.fixture
def _fake_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """Force is_extra_installed → False so tests behave consistently
    regardless of the dev venv's actual install state."""
    monkeypatch.setattr("fnd.extras.is_extra_installed", lambda _e: False)


@pytest.fixture
def _fake_installed(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """Force is_extra_installed → True."""
    monkeypatch.setattr("fnd.extras.is_extra_installed", lambda _e: True)
    monkeypatch.setattr("fnd.extras.actual_disk_mb", lambda _e: 900)
    monkeypatch.setattr("fnd.extras.installed_packages", lambda e: list(e.packages))


# 1 — Indexing screen surfaces structured-PDF rows


@pytest.mark.usefixtures("_fake_not_installed")
@pytest.mark.asyncio
async def test_indexing_screen_has_pdf_rows(built_index: Path, cfg: Config) -> None:
    from fnd.tui.menu import SECTION_PDF_TEXTURE
    from fnd.tui.settings_screen import SettingsList, open_settings_section

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_PDF_TEXTURE)
        await pilot.pause()
        lst = app.screen.query_one(SettingsList)
        ids = [it.id for it in lst._items]
        assert "pdf_texture.engine_status" in ids
        assert "pdf_texture.install" in ids


@pytest.mark.usefixtures("_fake_not_installed")
@pytest.mark.asyncio
async def test_status_row_not_installed(built_index: Path, cfg: Config) -> None:
    from fnd.tui.menu import SECTION_PDF_TEXTURE
    from fnd.tui.settings_screen import SettingsList, open_settings_section

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_PDF_TEXTURE)
        await pilot.pause()
        # Trailing value goes through lazy_trailing; wait a tick for the
        # worker thread to populate it.
        from fnd.tui.lazy_trailing import invalidate

        invalidate("pdf_texture.engine_status")
        lst = app.screen.query_one(SettingsList)
        row = next(it for it in lst._items if it.id == "pdf_texture.engine_status")
        # First call schedules the worker and returns "…"; second call
        # after a pause returns the real value.
        row.trailing_value(app)
        for _ in range(20):
            await pilot.pause()
            v = row.trailing_value(app)
            if "Not installed" in v:
                break
        else:
            v = row.trailing_value(app)
        assert "Not installed" in v
        assert "✗" in v


@pytest.mark.usefixtures("_fake_installed")
@pytest.mark.asyncio
async def test_install_label_flips_when_installed(built_index: Path, cfg: Config) -> None:
    from fnd.tui.menu import SECTION_PDF_TEXTURE
    from fnd.tui.settings_screen import SettingsList, open_settings_section

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_PDF_TEXTURE)
        await pilot.pause()
        lst = app.screen.query_one(SettingsList)
        row = next(it for it in lst._items if it.id == "pdf_texture.install")
        assert "Uninstall" in row.label


# 2 — Confirm screen chrome


@pytest.mark.usefixtures("_fake_not_installed")
@pytest.mark.asyncio
async def test_install_confirm_chrome_when_not_installed(built_index: Path, cfg: Config) -> None:
    from fnd.tui.menu import _open_pdf_install_confirm
    from fnd.tui.settings_screen import StructuredPdfConfirmScreen

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _open_pdf_install_confirm(app)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, StructuredPdfConfirmScreen)
        box = screen.query_one("#settings_box")
        assert "Install" in (box.border_title or "")
        # Not destructive on install path.
        assert not screen.has_class("-destructive")
        opts = screen.query_one("#confirm_list", OptionList)
        assert opts.option_count == 2
        hint = str(screen.query_one("#footer_hints", Static).content)
        assert "Nav" in hint
        assert "Confirm" in hint
        assert "Cancel" in hint


@pytest.mark.usefixtures("_fake_installed")
@pytest.mark.asyncio
async def test_uninstall_confirm_chrome_when_installed(built_index: Path, cfg: Config) -> None:
    from fnd.tui.menu import _open_pdf_install_confirm
    from fnd.tui.settings_screen import StructuredPdfConfirmScreen

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _open_pdf_install_confirm(app)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, StructuredPdfConfirmScreen)
        box = screen.query_one("#settings_box")
        assert "Uninstall" in (box.border_title or "")
        # Phase E: uninstall is recoverable severity → -recoverable class.
        assert screen.has_class("-recoverable")


# 3 — Body content covers the cost narrative


@pytest.mark.usefixtures("_fake_not_installed")
@pytest.mark.asyncio
async def test_install_disclosure_covers_costs(built_index: Path, cfg: Config) -> None:
    """Install body must mention disk + ML weights + per-PDF cost so
    the user knows what they're opting into. Phase E body uses the
    Outcome / Cost / Safety template — checks reflect that."""
    from fnd.tui.menu import _open_pdf_install_confirm

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _open_pdf_install_confirm(app)
        await pilot.pause()
        body = str(app.screen.query_one("#confirm_summary", Static).content)
        assert "Outcome" in body
        assert "Cost" in body
        assert "Safety" in body
        assert "MB" in body
        assert "ML" in body or "weights" in body
        assert "per PDF" in body or "s per PDF" in body
        assert "structured" in body


# 4 — Keyboard equivalence


@pytest.mark.usefixtures("_fake_not_installed")
@pytest.mark.asyncio
async def test_install_cancel_path(built_index: Path, cfg: Config) -> None:
    from fnd.tui.menu import _open_pdf_install_confirm
    from fnd.tui.settings_screen import StructuredPdfConfirmScreen

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _open_pdf_install_confirm(app)
        await pilot.pause()
        await pilot.press("down")  # cursor now on Cancel
        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, StructuredPdfConfirmScreen)


@pytest.mark.usefixtures("_fake_not_installed")
@pytest.mark.asyncio
async def test_install_esc_cancels(built_index: Path, cfg: Config) -> None:
    from fnd.tui.menu import _open_pdf_install_confirm
    from fnd.tui.settings_screen import StructuredPdfConfirmScreen

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _open_pdf_install_confirm(app)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, StructuredPdfConfirmScreen)


# 5 — Cross-section search


@pytest.mark.usefixtures("_fake_not_installed")
@pytest.mark.asyncio
async def test_search_finds_pdf_structure(built_index: Path, cfg: Config) -> None:
    from textual.widgets import Input

    from fnd.tui.menu import KIND_HEADER
    from fnd.tui.settings_screen import SettingsList, SettingsScreen

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await settings_ready(pilot, app)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        search = screen.query_one("#settings_search", Input)
        search.value = "pdf-structure"
        await pilot.pause()
        lst = screen.query_one(SettingsList)
        selectable = [it for it in lst._items if it.kind != KIND_HEADER]
        # Status + Install rows both match.
        assert any(it.id == "pdf_texture.engine_status" for it in selectable)
        assert any(it.id == "pdf_texture.install" for it in selectable)

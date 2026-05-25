"""Indexing settings section — tests follow docs/test_patterns/settings_screen.md.

Phase 1 surface: one auto-resume toggle row. Later steps extend this
section with structured-PDF status, install/uninstall, and cache rows.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from textual.widgets import Input, Static

from fnd.config import Config, Defaults, load
from fnd.index import build_index
from fnd.tui import FNDApp

if TYPE_CHECKING:
    pass


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.fixture
def cfg_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated config file so toggle writes don't touch the user's
    real settings. ``write_setting`` reads ``default_config_path`` for
    the path to mutate, so we redirect it."""
    p = tmp_path / "config.toml"
    p.write_text("")
    monkeypatch.setattr("fnd.config.default_config_path", lambda: p)
    return p


@pytest.fixture
def cfg(cfg_path: Path) -> Config:
    return load(cfg_path)


# 1 — Provider shape


def test_provider_indexing_has_auto_resume_toggle() -> None:
    from fnd.tui.menu import KIND_HEADER, KIND_TOGGLE, _provider_indexing

    rows = _provider_indexing(cast(FNDApp, _DummyApp()))
    by_id = {r.id: r for r in rows}
    assert "indexing.auto_resume" in by_id
    assert by_id["indexing.auto_resume"].kind == KIND_TOGGLE
    headers = [r for r in rows if r.kind == KIND_HEADER]
    assert headers, "section should group rows under at least one header"


def test_provider_indexing_toggle_round_trip() -> None:
    """Toggle getter reflects pydantic field; setter writes through."""
    from fnd.tui.menu import _provider_indexing

    cfg_on = Config(defaults=Defaults(indexer_auto_resume=True))
    cfg_off = Config(defaults=Defaults(indexer_auto_resume=False))
    app_on = cast(FNDApp, _DummyApp(config=cfg_on))
    app_off = cast(FNDApp, _DummyApp(config=cfg_off))
    rows_on = {r.id: r for r in _provider_indexing(app_on)}
    rows_off = {r.id: r for r in _provider_indexing(app_off)}
    getter_on = rows_on["indexing.auto_resume"].toggle_getter
    getter_off = rows_off["indexing.auto_resume"].toggle_getter
    assert getter_on is not None
    assert getter_off is not None
    assert getter_on(app_on) is True
    assert getter_off(app_off) is False


def test_indexer_auto_resume_persists(tmp_path: Path) -> None:
    """write_setting persists the field through pydantic validation."""
    from fnd.config import load, write_setting

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("")
    write_setting(config_path=cfg_path, dotted_path="defaults.indexer_auto_resume", value=False)
    assert load(cfg_path).defaults.indexer_auto_resume is False
    write_setting(config_path=cfg_path, dotted_path="defaults.indexer_auto_resume", value=True)
    assert load(cfg_path).defaults.indexer_auto_resume is True


# 1 — Chrome shape (pilot)


@pytest.mark.asyncio
async def test_indexing_subscreen_chrome(built_index: Path, cfg: Config) -> None:
    """Drilling into Indexing pushes a SettingsScreen with the right
    breadcrumb and hint bar."""
    from fnd.tui.menu import SECTION_INDEXING
    from fnd.tui.settings_screen import (
        SettingsList,
        SettingsScreen,
        open_settings_section,
    )

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_INDEXING)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        assert screen._breadcrumb == ("Indexing & PDF Texture",)
        assert screen.query_one("#settings_box")
        assert screen.query_one("#footer_hints")
        lst = screen.query_one(SettingsList)
        labels = [it.label for it in lst._items if it.is_selectable]
        assert "Auto-resume on launch" in labels


# 2 — Keyboard equivalence


@pytest.mark.asyncio
async def test_indexing_toggle_via_enter(built_index: Path, cfg: Config) -> None:
    """Enter on the toggle row flips the config value."""
    from fnd.tui.menu import SECTION_INDEXING
    from fnd.tui.settings_screen import SettingsList, open_settings_section

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_INDEXING)
        await pilot.pause()
        lst = app.screen.query_one(SettingsList)
        # Move cursor to the toggle row specifically — the Indexing
        # screen now has multiple selectable rows above it.
        for i, item in enumerate(lst._items):
            if item.id == "indexing.auto_resume":
                lst.cursor_index = i
                break
        assert app._config is not None
        before = app._config.defaults.indexer_auto_resume
        lst.action_activate()
        await pilot.pause()
        assert app._config is not None
        after = app._config.defaults.indexer_auto_resume
        assert before != after


# 3 — Hint-bar content


@pytest.mark.asyncio
async def test_indexing_hint_bar(built_index: Path, cfg: Config) -> None:
    from fnd.tui.menu import SECTION_INDEXING
    from fnd.tui.settings_screen import open_settings_section

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_INDEXING)
        await pilot.pause()
        text = str(app.screen.query_one("#footer_hints", Static).content)
        assert "Nav" in text
        assert "Back" in text


# 4 — Detail-strip content


@pytest.mark.asyncio
async def test_indexing_detail_strip_populated(built_index: Path, cfg: Config) -> None:
    """The auto-resume row has a description; detail strip mirrors it."""
    from fnd.tui.menu import SECTION_INDEXING
    from fnd.tui.settings_screen import SettingsList, open_settings_section
    from fnd.tui.widgets import DetailStrip

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_INDEXING)
        await pilot.pause()
        lst = app.screen.query_one(SettingsList)
        for i, item in enumerate(lst._items):
            if item.id == "indexing.auto_resume":
                lst.cursor_index = i
                break
        await pilot.pause()
        strip = app.screen.query_one(DetailStrip)
        assert strip._description, "auto-resume row should populate the detail strip"


# 6 — Cross-section search


@pytest.mark.asyncio
async def test_search_finds_auto_resume(built_index: Path, cfg: Config) -> None:
    from fnd.tui.menu import KIND_HEADER
    from fnd.tui.settings_screen import SettingsList, SettingsScreen

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        search = screen.query_one("#settings_search", Input)
        search.value = "auto-resume"
        await pilot.pause()
        lst = screen.query_one(SettingsList)
        labels = [it.label for it in lst._items if it.kind != KIND_HEADER]
        assert "Auto-resume on launch" in labels


# Root summary helper


def test_root_summary_reflects_toggle_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auto-resume on → ✓ glyph; off → ✗ glyph. Isolated cache dir so
    the real user cache doesn't bleed into the summary."""
    monkeypatch.setattr("fnd.cache.default_cache_dir", lambda: tmp_path / "cache")

    from fnd.tui.lazy_trailing import invalidate_all
    from fnd.tui.menu import _summary_indexing

    invalidate_all()

    cfg_on = Config(defaults=Defaults(indexer_auto_resume=True))
    cfg_off = Config(defaults=Defaults(indexer_auto_resume=False))
    on_summary = _summary_indexing(cast(FNDApp, _DummyApp(config=cfg_on)))
    off_summary = _summary_indexing(cast(FNDApp, _DummyApp(config=cfg_off)))
    assert "✓" in on_summary
    assert "auto-resume" in on_summary
    assert "✗" in off_summary
    assert "auto-resume" in off_summary


# ── Helpers ─────────────────────────────────────────────────────────


class _DummyApp:
    """Minimal stand-in for FNDApp used by pure provider tests.

    Provider functions read ``_config`` only; everything else is
    incidental. Avoids spinning up the full TUI for provider-shape
    tests."""

    def __init__(self, config: Config | None = None) -> None:
        self._config = config if config is not None else Config()
        self._highlights_enabled = True
        self._collections: list[str] = []

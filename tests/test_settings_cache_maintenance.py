"""Cache size display + Cache maintenance sub-screen tests.

Follows the test-pattern checklist in docs/test_patterns/settings_screen.md.
Cache prune/clear are exercised via direct callback invocation (the
confirm modal's OptionList is also pilot-tested via the Yes/Cancel
paths)."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import OptionList, Static

from fnd.cache import ExtractionCache
from fnd.config import Config, load
from fnd.extract.base import Block, Chunk
from fnd.index import build_index
from fnd.tui import FNDApp


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
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect default_cache_dir to a tmp dir so prune/clear tests
    don't touch the user's real cache."""
    root = tmp_path / "cache"
    monkeypatch.setattr("fnd.cache.default_cache_dir", lambda: root)
    return root


def _make_chunk(seq: int = 0) -> Chunk:
    return Chunk(
        parent_id="abc",
        path="/x.pdf",
        mtime=0,
        kind="pdf",
        body="b",
        body_struct=[Block(kind="p", text="b")],
        body_md="b",
        page=seq + 1,
        chunk_seq=seq,
    )


# 1 — Indexing screen surfaces cache rows


@pytest.mark.asyncio
async def test_indexing_screen_has_cache_rows(
    built_index: Path, cfg: Config, isolated_cache: Path
) -> None:
    from fnd.tui.menu import SECTION_INDEXING
    from fnd.tui.settings_screen import SettingsList, open_settings_section

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_INDEXING)
        await pilot.pause()
        lst = app.screen.query_one(SettingsList)
        ids = [it.id for it in lst._items]
        assert "indexing.cache_size" in ids
        assert "indexing.cache_maintenance" in ids


@pytest.mark.asyncio
async def test_cache_size_row_shows_empty_when_no_cache(
    built_index: Path, cfg: Config, isolated_cache: Path
) -> None:
    from fnd.tui.menu import SECTION_INDEXING
    from fnd.tui.settings_screen import SettingsList, open_settings_section

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_INDEXING)
        await pilot.pause()
        lst = app.screen.query_one(SettingsList)
        row = next(it for it in lst._items if it.id == "indexing.cache_size")
        assert row.trailing_value(app) == "empty"


@pytest.mark.asyncio
async def test_cache_size_row_shows_count_and_size(
    built_index: Path, cfg: Config, isolated_cache: Path
) -> None:
    cache = ExtractionCache(root=isolated_cache)
    cache.put("aa--v1", [_make_chunk(0)])
    cache.put("bb--v1", [_make_chunk(1)])

    from fnd.tui.menu import SECTION_INDEXING
    from fnd.tui.settings_screen import SettingsList, open_settings_section

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_INDEXING)
        await pilot.pause()
        lst = app.screen.query_one(SettingsList)
        row = next(it for it in lst._items if it.id == "indexing.cache_size")
        v = row.trailing_value(app)
        assert "2 entries" in v
        assert "B" in v or "KB" in v or "MB" in v


# 2 — Cache maintenance sub-screen chrome


@pytest.mark.asyncio
async def test_cache_maintenance_drill_chrome(
    built_index: Path, cfg: Config, isolated_cache: Path
) -> None:
    from fnd.tui.menu import SECTION_INDEXING
    from fnd.tui.settings_screen import SettingsList, SettingsScreen, open_settings_section

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_INDEXING)
        await pilot.pause()
        lst = app.screen.query_one(SettingsList)
        for i, it in enumerate(lst._items):
            if it.id == "indexing.cache_maintenance":
                lst.cursor_index = i
                break
        lst.action_activate()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        assert screen._breadcrumb == ("Indexing", "Cache maintenance")
        ids = [it.id for it in screen.query_one(SettingsList)._items]
        assert "cache.prune" in ids
        assert "cache.clear" in ids


# 3 — Confirm dialog chrome + keyboard


@pytest.mark.asyncio
async def test_clear_confirm_chrome(built_index: Path, cfg: Config, isolated_cache: Path) -> None:
    """Clear is destructive — confirm uses $error border and the
    irreversible callout. Mirrors DeleteCollectionScreen chrome
    (OptionList Yes/Cancel, hint bar at bottom)."""
    cache = ExtractionCache(root=isolated_cache)
    cache.put("aa--v1", [_make_chunk(0)])

    from fnd.tui.menu import _run_cache_clear
    from fnd.tui.settings_screen import CacheMaintenanceConfirm

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _run_cache_clear(app)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, CacheMaintenanceConfirm)
        assert screen.has_class("-destructive")
        assert screen.query_one("#confirm_irreversible")
        opts = screen.query_one("#confirm_list", OptionList)
        assert opts.option_count == 2
        hint = str(screen.query_one("#footer_hints", Static).content)
        assert "Nav" in hint
        assert "Confirm" in hint
        assert "Cancel" in hint


# 4 — Keyboard equivalence (arrows + enter)


@pytest.mark.asyncio
async def test_clear_cancel_path(built_index: Path, cfg: Config, isolated_cache: Path) -> None:
    """Pressing Down then Enter on the confirm dialog selects Cancel —
    no side effect."""
    cache = ExtractionCache(root=isolated_cache)
    cache.put("aa--v1", [_make_chunk(0)])

    from fnd.tui.menu import _run_cache_clear

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _run_cache_clear(app)
        await pilot.pause()
        # Cursor defaults to Yes — move down to Cancel and select.
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        # Cache untouched.
        assert isolated_cache.exists()
        assert cache.entry_count() == 1


@pytest.mark.asyncio
async def test_clear_yes_path(built_index: Path, cfg: Config, isolated_cache: Path) -> None:
    """Pressing Enter on the highlighted Yes option clears the cache."""
    cache = ExtractionCache(root=isolated_cache)
    cache.put("aa--v1", [_make_chunk(0)])
    cache.put("bb--v1", [_make_chunk(1)])

    from fnd.tui.menu import _run_cache_clear

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _run_cache_clear(app)
        await pilot.pause()
        await pilot.press("enter")  # Yes is highlighted by default
        await pilot.pause()
        assert not isolated_cache.exists() or cache.entry_count() == 0


# 5 — Esc cancels without side effect


@pytest.mark.asyncio
async def test_clear_esc_cancels(built_index: Path, cfg: Config, isolated_cache: Path) -> None:
    cache = ExtractionCache(root=isolated_cache)
    cache.put("aa--v1", [_make_chunk(0)])

    from fnd.tui.menu import _run_cache_clear

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _run_cache_clear(app)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert cache.entry_count() == 1


# 6 — Prune notifies cleanly when no stale entries


@pytest.mark.asyncio
async def test_prune_no_stale_notifies_only(
    built_index: Path, cfg: Config, isolated_cache: Path
) -> None:
    """When all entries are fresh, prune notifies and doesn't push a
    confirm dialog."""
    from fnd.cache import ExtractionCache
    from fnd.extract.pdf import _extractor_signature

    sig = _extractor_signature()
    cache = ExtractionCache(root=isolated_cache)
    cache.put(f"aa--{sig}", [_make_chunk(0)])

    from fnd.tui.menu import _run_cache_prune
    from fnd.tui.settings_screen import CacheMaintenanceConfirm

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _run_cache_prune(app)
        await pilot.pause()
        # Confirm dialog should NOT be on top — no stale to prune.
        assert not isinstance(app.screen, CacheMaintenanceConfirm)


@pytest.mark.asyncio
async def test_prune_with_stale_opens_confirm(
    built_index: Path, cfg: Config, isolated_cache: Path
) -> None:
    """Stale-signature entries → confirm dialog appears."""
    cache = ExtractionCache(root=isolated_cache)
    cache.put("aa--stale_sig_xx", [_make_chunk(0)])
    cache.put("bb--stale_sig_xx", [_make_chunk(1)])

    from fnd.tui.menu import _run_cache_prune
    from fnd.tui.settings_screen import CacheMaintenanceConfirm

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        _run_cache_prune(app)
        await pilot.pause()
        assert isinstance(app.screen, CacheMaintenanceConfirm)
        # Prune is recoverable — not destructive.
        assert not app.screen.has_class("-destructive")


# 7 — Root summary reflects cache state


def test_root_summary_includes_cache(isolated_cache: Path, cfg: Config) -> None:
    cache = ExtractionCache(root=isolated_cache)
    cache.put("aa--v1", [_make_chunk(0)])

    from typing import cast

    from fnd.tui.menu import _summary_indexing

    class _App:
        def __init__(self) -> None:
            self._config = cfg

    summary = _summary_indexing(cast(FNDApp, _App()))
    assert "cache" in summary
    assert "auto-resume" in summary

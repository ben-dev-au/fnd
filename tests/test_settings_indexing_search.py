"""Cross-section search coverage for the new Indexing rows.

The settings filter walks every section via walk_all_sections and
ranks rows by label / key / keywords / breadcrumb segments. Verify
each common keyword landed on the rows that should match.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Input

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.menu import KIND_HEADER
from fnd.tui.settings_screen import SettingsList, SettingsScreen


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


@pytest.mark.parametrize(
    ("query", "expected_id"),
    [
        ("auto-resume", "indexing.auto_resume"),
        # PDF Texturising + cache split into the dedicated PDF Texture
        # sibling section, so its ids now live under `pdf_texture.*`.
        # Cross-section search must still surface them.
        ("pdf-structure", "pdf_texture.engine_status"),
        ("pdf-structure", "pdf_texture.install"),
        ("cache", "pdf_texture.cache_size"),
        ("prune", "pdf_texture.cache_prune"),
        ("clear", "pdf_texture.cache_clear"),
        ("wipe", "pdf_texture.cache_clear"),
        ("stale", "pdf_texture.cache_prune"),
        ("docling", "pdf_texture.install"),
        ("pymupdf4llm", "pdf_texture.install"),
    ],
)
@pytest.mark.asyncio
async def test_search_finds_indexing_row(
    built_index: Path, cfg: Config, query: str, expected_id: str
) -> None:
    """One row per keyword should appear in the filtered list."""
    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        search = screen.query_one("#settings_search", Input)
        search.value = query
        await pilot.pause()
        lst = screen.query_one(SettingsList)
        selectable = [it for it in lst._items if it.kind != KIND_HEADER]
        ids = {it.id for it in selectable}
        assert expected_id in ids, f"search {query!r} should find {expected_id!r}; got {ids}"


@pytest.mark.asyncio
async def test_search_breadcrumb_is_indexing(built_index: Path, cfg: Config) -> None:
    """Indexing rows should carry an Indexing breadcrumb so the user
    sees the section context in the filtered view."""
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
        for item in lst._items:
            if item.id == "indexing.auto_resume":
                bc = screen._search_breadcrumbs.get(id(item))
                assert bc == ("Indexing",), f"expected Indexing breadcrumb; got {bc}"
                return
        pytest.fail("indexing.auto_resume not in filtered results")

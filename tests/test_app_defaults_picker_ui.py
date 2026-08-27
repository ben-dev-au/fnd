"""End-to-end coverage for the per-filetype app-default pickers that
live under Settings → Preferences. The existing
``tests/test_app_defaults_picker.py`` covers the choices/getter/setter
helpers in isolation; this file checks the rows actually appear in
the Preferences section and round-trip through the picker."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.config import CollectionConfig, Config, SourceConfig
from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.menu import _FILETYPE_LABELS
from tests._pilot_wait import settings_ready


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


def _seed(fixtures_dir: Path) -> Config:
    return Config(
        collections={"default": CollectionConfig(sources=[SourceConfig(path=fixtures_dir)])}
    )


@pytest.mark.asyncio
async def test_preferences_lists_one_picker_per_filetype(
    built_index: Path, fixtures_dir: Path
) -> None:
    """One ``Default <label> app`` picker row per indexer-supported kind,
    grouped under the ``Default app per filetype`` header."""
    from fnd.tui.menu import SECTION_PREFERENCES
    from fnd.tui.settings_screen import (
        SettingsList,
        SettingsScreen,
        open_settings_section,
    )

    app = FNDApp(index_dir=built_index, config=_seed(fixtures_dir))
    async with app.run_test() as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_PREFERENCES)
        await settings_ready(pilot, app)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        lst = screen.query_one(SettingsList)
        labels = [it.label for it in lst._items]
        # Header is present.
        assert "Default app per filetype" in labels
        # One row per filetype, in declaration order.
        for kind, label in _FILETYPE_LABELS.items():
            row_label = f"Default {label} app"
            assert row_label in labels, (kind, labels)
            row = next(it for it in lst._items if it.label == row_label)
            assert row.kind == "picker", row.kind


@pytest.mark.asyncio
async def test_app_default_picker_round_trip_persists(
    built_index: Path,
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driving the picker setter writes the chosen app id to the
    [app_defaults] table and a subsequent ``load()`` reflects it."""
    from fnd.config import load
    from fnd.tui.menu import _set_app_default_for_kind

    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    # Touch a config so load() returns something usable.
    cfg_path.write_text("")

    app = FNDApp(index_dir=built_index, config=_seed(fixtures_dir))
    async with app.run_test() as pilot:
        await pilot.pause()
        app._config = load()
        _set_app_default_for_kind(app, "md", "system")
        reloaded = load()
        assert reloaded.app_defaults.get("md") == "system"

        # The empty-string sentinel (auto-resolve) clears the entry.
        _set_app_default_for_kind(app, "md", "")
        reloaded = load()
        assert reloaded.app_defaults.get("md", "") == ""

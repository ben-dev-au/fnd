"""The custom-glob editor's label must not wrap over its own field.

Measured at 100 cols: the label Static rendered 4 rows inside a 2-row bar, so
the input was clipped and the typed value painted at the end of the wrapped
help sentence rather than in a field of its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Static

from fnd.config import CollectionConfig, SourceConfig, load, write_collection
from fnd.tui import FNDApp
from fnd.tui.settings_screen import EditBar, SourceFormScreen
from tests._pilot_wait import screen_ready


@pytest.fixture
def probe_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    root = tmp_path / "vault"
    root.mkdir()
    write_collection(
        config_path=cfg_path,
        name="probe",
        collection=CollectionConfig(sources=[SourceConfig(path=root)]),
    )
    return cfg_path


async def _open_glob_editor(app: FNDApp, pilot: Any) -> SourceFormScreen:
    app._config = load()
    app.push_screen(SourceFormScreen(collection_name="probe", source_index=0))
    await screen_ready(pilot, app, SourceFormScreen)
    screen = app.screen
    assert isinstance(screen, SourceFormScreen)
    screen._set_excludes(["__custom__"])
    await pilot.pause()
    return screen


@pytest.mark.asyncio
async def test_the_label_stays_on_one_row(probe_config: Path, tmp_path: Path) -> None:
    app = FNDApp(index_dir=tmp_path / "idx")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        screen = await _open_glob_editor(app, pilot)

        bar = screen.query_one(EditBar)
        label = bar.query_one(".-edit-label", Static)
        assert label.region.height == 1, (
            f"the label wrapped to {label.region.height} rows in a {bar.region.height}-row bar"
        )
        assert label.region.height <= bar.region.height


@pytest.mark.asyncio
async def test_the_field_still_paints_what_is_typed(probe_config: Path, tmp_path: Path) -> None:
    """The control: capping the label must not clip the field away."""
    app = FNDApp(index_dir=tmp_path / "idx")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _open_glob_editor(app, pilot)
        await pilot.press("z", "z", "z")
        await pilot.pause()

        rows = ["".join(s.text for s in strip) for strip in app.screen._compositor.render_strips()]
        assert any("zzz" in r for r in rows), f"the typed value painted nowhere: {rows[-4:]}"

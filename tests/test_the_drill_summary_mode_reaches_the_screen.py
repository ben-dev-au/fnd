"""`Drill row summaries` saved, showed itself as saved, and did nothing.

The mode was implemented on `MenuItem.trailing_value`, which the renderer never
calls: `_trailing_segments` reads `value_getter` directly. Its only callers
were tests, one of which asserted `always_ellipsis` renders `…` and passed
against a screen that has never done so.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from fnd.config import load
from fnd.tui import FNDApp
from fnd.tui.settings_screen import SettingsScreen, open_settings
from tests._pilot_wait import settings_ready


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> Any:
    cfg_path = tmp_path / f"config-{mode}.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [defaults]
            drill_summary_mode = "{mode}"

            [[collections.notes.sources]]
            path = "{tmp_path.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


async def _root_rows(app: FNDApp, pilot: Any) -> str:
    open_settings(app)
    await settings_ready(pilot, app)
    screen = app.screen
    assert isinstance(screen, SettingsScreen)
    rows = ["".join(s.text for s in strip) for strip in screen._compositor.render_strips()]
    return "\n".join(r for r in rows if "Collections" in r or "Preferences" in r)


@pytest.mark.asyncio
async def test_always_ellipsis_hides_the_summary(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path, monkeypatch, "always_ellipsis")
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        app._config = cfg
        painted = await _root_rows(app, pilot)

    assert "collection" not in painted.lower().replace("collections ", ""), painted
    assert "…" in painted, painted


@pytest.mark.asyncio
async def test_always_show_keeps_it(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: the default must still carry the summary it is for."""
    cfg = _config(tmp_path, monkeypatch, "always_show")
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        app._config = cfg
        painted = await _root_rows(app, pilot)

    assert "1 collection" in painted, painted

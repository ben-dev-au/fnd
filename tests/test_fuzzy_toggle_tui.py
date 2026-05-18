"""TUI-level binding for ``toggle_fuzzy``.

The action writes the new value through ``write_setting`` so it
survives restarts. ``ctrl+f`` works from query-bar focus (Textual
doesn't consume control combos for plain Input widgets).
"""

from __future__ import annotations

import textwrap
import tomllib
from pathlib import Path

import pytest
from textual.widgets import Input

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp


def _write(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture
def cfg_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(
        textwrap.dedent("""
            [[collections.notes.sources]]
            path = "/tmp/notes"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: p)
    return p


@pytest.fixture
def cfg(cfg_path: Path) -> Config:
    return load(cfg_path)


@pytest.fixture
def md_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    a = tmp_path / "notes"
    _write(a / "Notes.md", "# Patterns\n\nThe templates pattern is described here.\n")
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_toggle_fuzzy_flips_config_and_persists(
    cfg: Config, cfg_path: Path, md_index: Path
) -> None:
    """Invoking the action flips the config field and writes it to disk."""
    app = FNDApp(index_dir=md_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._config is not None
        assert app._config.defaults.fuzzy_enabled is True
        app.action_toggle_fuzzy()
        await pilot.pause()
        assert app._config.defaults.fuzzy_enabled is False
        # And on disk:
        on_disk = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
        assert on_disk["defaults"]["fuzzy_enabled"] is False
        # Toggle back.
        app.action_toggle_fuzzy()
        await pilot.pause()
        assert app._config.defaults.fuzzy_enabled is True


@pytest.mark.asyncio
async def test_ctrl_f_fires_toggle_from_query_bar(
    cfg: Config, cfg_path: Path, md_index: Path
) -> None:
    """The default binding fires from query-bar focus.

    Textual's Input binds ctrl+f to "delete right word"; the action
    registry marks this binding ``priority=True`` so it overrides
    Input's handler and the toggle stays reachable while the query
    bar is focused."""
    app = FNDApp(index_dir=md_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.query_one("#query_bar", Input)
        bar.focus()
        await pilot.pause()
        assert app._config is not None
        before = app._config.defaults.fuzzy_enabled
        await pilot.press("ctrl+f")
        await pilot.pause()
        assert app._config.defaults.fuzzy_enabled is not before

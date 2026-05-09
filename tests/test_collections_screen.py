"""Phase 5.5e-3: TUI Collections form — F3 / :collections."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Static

from acorn.config import Config, load
from acorn.tui import AcornApp


@pytest.fixture
def cfg_with_one_collection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.notes.sources]]
            path = "/tmp/notes"
            includes = ["**/*.md"]
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("acorn.config.default_config_path", lambda: cfg_path)
    monkeypatch.setattr("acorn.cli.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.mark.asyncio
async def test_f3_opens_collections_screen(
    cfg_with_one_collection: Config, tmp_index_dir: Path
) -> None:
    app = AcornApp(index_dir=tmp_index_dir, config=cfg_with_one_collection)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        # The screen mounts a Static with the title "Collections".
        # After push_screen, app.screen is the CollectionsScreen — query there.
        title = app.screen.query_one("#collections_title", Static)
        assert "collections" in str(title.content).lower()


@pytest.mark.asyncio
async def test_escape_closes_collections_screen(
    cfg_with_one_collection: Config, tmp_index_dir: Path
) -> None:
    app = AcornApp(index_dir=tmp_index_dir, config=cfg_with_one_collection)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        assert app.screen.query("#collections_title")
        await pilot.press("escape")
        await pilot.pause()
        # After dismiss, app.screen is back to the main screen (no #collections_title).
        assert not app.screen.query("#collections_title")

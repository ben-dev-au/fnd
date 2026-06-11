"""A malformed query surfaces a calm inline notice below the query bar and
never crashes the app; a valid query clears it."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Static

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp


def _write(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    p = tmp_path / "config.toml"
    p.write_text(
        textwrap.dedent("""
            [[collections.notes.sources]]
            path = "/tmp/notes"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: p)
    return load(p)


@pytest.fixture
def md_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    a = tmp_path / "notes"
    _write(a / "Notes.md", "# Patterns\n\nThe templates pattern is described here.\n")
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_malformed_proximity_notice_then_clears(cfg: Config, md_index: Path) -> None:
    app = FNDApp(index_dir=md_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        app._search.run("{60}")
        await pilot.pause()
        notice = app.query_one("#query_notice", Static)
        assert notice.display is True
        assert "proximity" in str(notice.render()).lower()

        app._search.run("templates")
        await pilot.pause()
        assert app.query_one("#query_notice", Static).display is False

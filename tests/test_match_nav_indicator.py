"""The match-nav k/N indicator lives on the preview pane's bottom border
(``border_subtitle``) — where the matches are — so it reads as part of the
preview and can't be clipped off the crowded global footer line.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Tree

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.preview_scrollbar import MatchAwareScroll
from tests._pilot_wait import wait_until


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.notes.sources]]
            path = "/tmp/notes"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.fixture
def match_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    a = tmp_path / "notes"
    a.mkdir(parents=True, exist_ok=True)
    (a / "Notes.md").write_text(
        "# CRC notes\n\nThe frame uses a CRC checksum.\n\nAnother CRC mention here.\n",
        encoding="utf-8",
    )
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_match_indicator_on_preview_bottom_border(cfg: Config, match_index: Path) -> None:
    app = FNDApp(index_dir=match_index, config=cfg, collection="notes", initial_query="CRC")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.query_one("#results_pane", Tree).focus()
        await wait_until(
            pilot,
            lambda: app._match_nav.count >= 1,
            timeout=30.0,
            message="match-nav count never populated",
        )
        pane = app.query_one("#preview_pane", MatchAwareScroll)
        subtitle = str(pane.border_subtitle or "")
        assert "match" in subtitle, f"no indicator on preview border_subtitle: {subtitle!r}"
        assert str(app._match_nav.count) in subtitle, f"count missing from indicator: {subtitle!r}"

        # It clears in Reading View (which drops the border).
        app.action_toggle_reading_mode()
        await pilot.pause()
        assert "match" not in str(app.query_one("#preview_pane").border_subtitle or "")

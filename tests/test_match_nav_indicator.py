"""Match navigation surfaces in two places, deliberately separate:

* the ``n/b`` KEY hint lives in the footer keybinding area (like every key), and
* the ``match k/N`` COUNT lives on the preview pane's bottom border
  (``border_subtitle``) — where the matches are, and where it can't be clipped
  off the crowded footer line.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from rich.cells import cell_len
from textual.widgets import Static, Tree

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.preview_scrollbar import MatchAwareScroll
from tests._pilot_wait import wait_until


def _visible_footer(app: FNDApp) -> str:
    """The footer text that actually fits within the footer widget's width."""
    ft = app.query_one("#footer_hints", Static)
    plain = ft.render().plain  # type: ignore[union-attr]
    width = ft.size.width
    out, acc = "", 0
    for ch in plain:
        w = cell_len(ch)
        if acc + w > width:
            break
        out += ch
        acc += w
    return out


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
        # COUNT on the preview's bottom border.
        pane = app.query_one("#preview_pane", MatchAwareScroll)
        subtitle = str(pane.border_subtitle or "")
        assert "match" in subtitle, f"no count on preview border_subtitle: {subtitle!r}"
        assert str(app._match_nav.count) in subtitle, f"count missing from indicator: {subtitle!r}"

        # KEY hint in the footer keybinding area, and actually on screen.
        assert "n/b" in _visible_footer(app), (
            f"n/b key hint clipped/missing from footer: {_visible_footer(app)!r}"
        )

        # Both clear in Reading View (border dropped; n/b inert there).
        app.action_toggle_reading_mode()
        await pilot.pause()
        assert "match" not in str(app.query_one("#preview_pane").border_subtitle or "")

"""The match-nav k/N indicator must be VISIBLE in the footer — i.e. within the
footer widget's rendered width — not merely present in the (overflowing)
content string. The footer line overflows a normal terminal, so an item
appended at the end is clipped off-screen; the indicator is placed first in the
contextual cluster to stay on screen.
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


@pytest.mark.asyncio
async def test_match_indicator_is_within_visible_footer_width(
    cfg: Config, match_index: Path
) -> None:
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
        # The indicator is on screen (within the footer's rendered width),
        # not clipped past the right edge.
        assert "match" in _visible_footer(app), (
            f"indicator clipped off-screen; visible footer = {_visible_footer(app)!r}"
        )

"""End-to-end: a wildcard query, driven through the real TUI, must colour the
matched word in the preview as literal-prefix-yellow + wildcard-fill-orange —
not the all-orange regression. Exercises the live query → MatchSpec → highlight
path (the unit tests build the spec directly; this proves the app does too).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Input

from fnd.config import Config, load
from fnd.index import build_index
from fnd.render import HIGHLIGHT_STYLE, MISMATCH_STYLE, word_highlight_runs
from fnd.tui import FNDApp


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
    a.mkdir(parents=True, exist_ok=True)
    (a / "Notes.md").write_text(
        "# Pricing\n\nThe strategy gave a big discount today.\n", encoding="utf-8"
    )
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_wildcard_match_colours_prefix_yellow_fill_orange(
    cfg: Config, md_index: Path
) -> None:
    app = FNDApp(index_dir=md_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.query_one("#query_bar", Input)
        bar.value = "strategy discoun*"
        await pilot.press("enter")
        await pilot.pause()
        spec = app._current_match_spec
        # The live spec must mark the wildcard match...
        from fnd.matching import word_matches

        assert word_matches("discount", spec), "live app spec did not match the wildcard word"
        # ...and colour it discoun(yellow) + t(orange), NOT all-orange against "strategy".
        runs = word_highlight_runs("discount", spec)
        assert runs == [(0, 7, HIGHLIGHT_STYLE), (7, 8, MISMATCH_STYLE)], runs

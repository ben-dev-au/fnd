"""End-to-end: n/b navigate between the two CRC matches in a flashcards
table taller than the viewport, in the real FNDApp.

The reported bug: the second match sits below the fold with nothing pointing
to it. This proves n reaches it and n again wraps back to the first.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Tree

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp
from tests._pilot_wait import wait_until


def _write(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


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
def flashcards_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """One file whose single chunk is a Q&A table taller than a test viewport,
    with CRC in card 32's answer and card 47's question — far apart."""
    a = tmp_path / "notes"
    rows = "".join(
        f"| {i} | question {i} filler filler | answer {i} filler filler |\n" for i in range(1, 32)
    )
    rows += "| 32 | Ethernet Type II Frame | link-layer frame with a CRC checksum |\n"
    rows += "".join(
        f"| {i} | question {i} filler filler | answer {i} filler filler |\n" for i in range(33, 47)
    )
    rows += "| 47 | What is the Ethernet CRC field | Cyclic Redundancy Check field |\n"
    body = "# Flashcards\n\n| # | Q | A |\n| --- | --- | --- |\n" + rows
    _write(a / "Cards.md", body)
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_n_navigates_between_off_screen_table_matches(
    cfg: Config, flashcards_index: Path
) -> None:
    app = FNDApp(
        index_dir=flashcards_index,
        config=cfg,
        collection="notes",
        initial_query="CRC",
    )
    async with app.run_test(size=(110, 24)) as pilot:
        await pilot.pause()
        app.query_one("#results_pane", Tree).focus()
        # Preview mounts, then match-nav rebuilds to both CRC cells.
        await wait_until(
            pilot,
            lambda: app._match_nav.count == 2,
            timeout=30.0,
            message="match-nav did not find both table matches",
        )
        pane = app.query_one("#preview_pane", VerticalScroll)
        # The auto-reveal landed on the first match (card 32); the second
        # (card 47) is below the fold.
        start_y = pane.scroll_offset.y

        app.action_nav_next_match()  # n → jump down to card 47
        await pilot.pause()
        after_n = pane.scroll_offset.y
        assert after_n > start_y, (start_y, after_n)
        assert app._match_nav.position == 2

        app.action_nav_next_match()  # n → wrap back up to card 32
        await pilot.pause()
        assert pane.scroll_offset.y < after_n
        assert app._match_nav.position == 1

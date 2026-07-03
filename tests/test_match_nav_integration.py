"""End-to-end: n/b navigate between the two CRC matches in a flashcards
table taller than the viewport, in the real FNDApp — including reaching the
second match cell that sits below the fold in a multi-section document.

The reported bug: after the multi-chunk preview settled, the navigator held a
stale (empty) snapshot of stops, so n did nothing and the off-screen second
match was unreachable. These tests assert the second cell actually becomes
visible, not merely that the scroll offset moved.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.containers import VerticalScroll
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Tree

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
    """A multi-section file (several headings/paragraphs, so the preview mounts
    as multiple chunks and reflows) ending in a Q&A table taller than a test
    viewport, with CRC in card 32's answer and card 47's question."""
    a = tmp_path / "notes"
    rows = "".join(
        f"| {i} | question {i} filler filler | answer {i} filler filler |\n" for i in range(1, 32)
    )
    rows += "| 32 | Ethernet Type II Frame | link-layer frame with a CRC checksum |\n"
    rows += "".join(
        f"| {i} | question {i} filler filler | answer {i} filler filler |\n" for i in range(33, 47)
    )
    rows += "| 47 | What is the Ethernet CRC field | Cyclic Redundancy Check field |\n"
    body = (
        "# Networking Notes\n\n"
        "## Overview\n\nThe frame check uses a CRC value for integrity.\n\n"
        "## Detail\n\nSome unrelated prose about switches and routers here.\n\n"
        "## More Detail\n\nMore unrelated prose about addressing and subnets.\n\n"
        "## Study Flashcards\n\n| # | Q | A |\n| --- | --- | --- |\n" + rows
    )
    _write(a / "Cards.md", body)
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


def _cell_visible(pane: VerticalScroll, dt: DataTable, coord: Coordinate) -> bool:
    """True if the table cell at ``coord`` is within the pane's visible area."""
    try:
        cell = dt._get_cell_region(coord)  # type: ignore[attr-defined]
    except Exception:
        return False
    if cell.height == 0:
        return False
    screen = cell.translate(dt.region.offset - dt.scroll_offset)
    vis = pane.scrollable_content_region
    return screen.y >= vis.y and screen.y < vis.y + vis.height


@pytest.mark.asyncio
async def test_n_reaches_second_table_match_below_the_fold(
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
        # The preview settles and the navigator sees every CRC match (the intro
        # sentence + both table cells).
        await wait_until(
            pilot,
            lambda: app._match_nav.count >= 3,
            timeout=30.0,
            message="match-nav did not enumerate all matches after settle",
        )
        pane = app.query_one("#preview_pane", VerticalScroll)
        dt = next(t for t in pane.query(DataTable) if getattr(t, "_fnd_match_coords", []))
        card47 = Coordinate(46, 1)

        # Card 47 starts below the fold — unreachable without navigation.
        assert not _cell_visible(pane, dt, card47), "card 47 unexpectedly already visible"

        # Press n until it comes into view (a few hops through the earlier matches).
        for _ in range(app._match_nav.count + 1):
            if _cell_visible(pane, dt, card47):
                break
            app.action_nav_next_match()
            await pilot.pause()
            await pilot.pause()
        assert _cell_visible(pane, dt, card47), "n never revealed card 47's match cell"

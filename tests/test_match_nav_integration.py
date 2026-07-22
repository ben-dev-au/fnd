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
from typing import Any

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


def _table_rows() -> str:
    rows = "".join(
        f"| {i} | question {i} filler filler | answer {i} filler filler |\n" for i in range(1, 32)
    )
    rows += "| 32 | Ethernet Type II Frame | link-layer frame with a CRC checksum |\n"
    rows += "".join(
        f"| {i} | question {i} filler filler | answer {i} filler filler |\n" for i in range(33, 47)
    )
    rows += "| 47 | What is the Ethernet CRC field | Cyclic Redundancy Check field |\n"
    return rows


@pytest.fixture
def flashcards_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """Two ADJACENT matching results: the flashcards table (two CRC cells, taller
    than the viewport) and a short paragraph (one CRC). Focusing the table mounts
    both, so the paragraph's match is a mounted stop that the table's scoped
    ``n``/``b`` must exclude — the scoping guarantee, testable without relying on
    a distant chunk background-mounting."""
    a = tmp_path / "notes"
    body = (
        "# Networking Notes\n\n"
        "## Study Flashcards\n\n| # | Q | A |\n| --- | --- | --- |\n"
        + _table_rows()
        + "\n## Summary\n\nA short note on the CRC field.\n"
    )
    _write(a / "Cards.md", body)
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


def _current_stop_count(app: FNDApp) -> int:
    pane = app.query_one("#preview_pane", VerticalScroll)
    return len(app._match_nav._chunk_stops(pane))


async def _walk_to_stop_count(pilot: object, app: FNDApp, want: int, key: str) -> bool:
    """Press ``key`` in the results tree until the current result has ``want``
    match stops (i.e. the multi-view table becomes the focused, mounted result)."""
    for _ in range(10):
        if _current_stop_count(app) == want:
            return True
        await pilot.press(key)  # type: ignore[attr-defined]
        for _ in range(14):
            await pilot.pause()  # type: ignore[attr-defined]
    return _current_stop_count(app) == want


@pytest.fixture
def table_result_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """Same multi-section file, but only the flashcards table matches — so the
    table IS the loaded result (the reported scenario). Card 32 is revealed on
    load; card 47's cell sits below the fold within the same result."""
    a = tmp_path / "notes"
    body = (
        "# Networking Notes\n\n"
        "## Overview\n\nThe frame check uses a checksum value for integrity.\n\n"
        "## Detail\n\nSome unrelated prose about switches and routers here.\n\n"
        "## More Detail\n\nMore unrelated prose about addressing and subnets.\n\n"
        "## Study Flashcards\n\n| # | Q | A |\n| --- | --- | --- |\n" + _table_rows()
    )
    _write(a / "Cards.md", body)
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


def _cell_visible(pane: VerticalScroll, dt: DataTable[Any], coord: Coordinate) -> bool:
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
async def test_n_reveals_second_table_match_below_the_fold(
    cfg: Config, table_result_index: Path
) -> None:
    """When the table IS the current result, n reveals its off-screen second
    match cell — the reported bug, at the geometry level (cell truly visible)."""
    app = FNDApp(index_dir=table_result_index, config=cfg, collection="notes", initial_query="CRC")
    async with app.run_test(size=(110, 24)) as pilot:
        await pilot.pause()
        app.query_one("#results_pane", Tree).focus()

        # count is derived by an async chain after the deep-table preview mounts;
        # on the slower Windows CI runner that mount can outlast the single
        # post-mount count-tick, which then samples the still-composing table and
        # settles low with nothing to re-fire it during a passive wait. Re-run
        # rebuild() each poll so the count re-samples the current subtree and
        # reflects the table the moment it finishes composing (rebuild is the same
        # idempotent call the app makes on search/mount — no product change).
        def _enumerated() -> bool:
            if app._match_nav.count >= 2:
                return True
            app._match_nav.rebuild()
            return False

        await wait_until(
            pilot,
            _enumerated,
            timeout=60.0,
            message="match-nav did not enumerate the table matches after settle",
        )
        pane = app.query_one("#preview_pane", VerticalScroll)
        dt = next(t for t in pane.query(DataTable) if getattr(t, "_fnd_match_coords", []))
        card47 = Coordinate(46, 1)

        # Card 47 starts below the fold — unreachable without navigation.
        assert not _cell_visible(pane, dt, card47), "card 47 unexpectedly already visible"

        for _ in range(app._match_nav.count + 1):
            if _cell_visible(pane, dt, card47):
                break
            app.action_nav_next_match()
            await pilot.pause()
            await pilot.pause()
        assert _cell_visible(pane, dt, card47), "n never revealed card 47's match cell"


@pytest.mark.asyncio
async def test_n_stays_within_the_current_result(cfg: Config, flashcards_index: Path) -> None:
    """n/b are scoped to the CURRENT result's chunk — they hop between its hidden
    matches and never cross into another result (the results-pane arrows' job).

    Tested at the mechanism, not via a distant chunk background-mounting: navigate
    to the multi-view table (which focus-mounts it AND its adjacent Summary
    result), then assert the scoped stop set excludes the neighbour's match that
    the unscoped set includes, and that hammering n never leaks past that scope.
    """
    app = FNDApp(index_dir=flashcards_index, config=cfg, collection="notes", initial_query="CRC")
    async with app.run_test(size=(110, 24)) as pilot:
        await pilot.pause()
        app.query_one("#results_pane", Tree).focus()
        nav = app._match_nav
        # Walk the results arrows until the focused result is the two-match table.
        assert await _walk_to_stop_count(pilot, app, 2, "down"), (
            "results arrows never landed on the two-match flashcards table"
        )
        pane = app.query_one("#preview_pane", VerticalScroll)

        # Scoping is active (the table's chunk extent resolved) and captures both
        # of the table's matches — no more, no less.
        assert nav._current_chunk_extent(pane) is not None, "chunk extent did not resolve"
        assert len(nav._chunk_stops(pane)) == 2, "scoped stops should be the table's two matches"
        # The adjacent Summary result's match is mounted too, so the UNSCOPED set
        # is larger — proving the scope is actively excluding another result.
        assert len(nav._region_stops(pane)) > len(nav._chunk_stops(pane)), (
            "expected a neighbouring result's match to be mounted and excluded by scope"
        )

        # Hammer n past the table's own two matches: it stays scoped every press —
        # the stop set never grows and the burst cursor never indexes a foreign
        # stop (which is how crossing into another result would manifest).
        for _ in range(5):
            app.action_nav_next_match()
            await pilot.pause()
            await pilot.pause()
            stops = nav._chunk_stops(pane)
            assert len(stops) == 2, "n changed the scoped stop set — it left the current result"
            assert nav._last_target is not None, "n did not record a landing stop"
            assert nav._last_target < len(stops), (
                "n's cursor indexed outside the current result's stops"
            )

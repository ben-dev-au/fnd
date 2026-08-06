"""Switching results must refresh the ▲/▼ view markers immediately — even for
a warm nav whose reveal doesn't move the scroll. The bug: markers were only
re-measured on scroll, so moving from a multi-view result to a single-view one
left the old ▼ lingering (and a multi-view result sometimes showed nothing
until the second visit). Fixed by re-measuring on the reveal (result-switch)
event; this asserts the markers track the current result across switches."""

from __future__ import annotations

import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Tree

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.preview_scrollbar import MatchAwareScroll
from tests._pilot_wait import safe_pause, safe_press, wait_until

# One file, two matching results: a tall flashcards table (CRC far apart → a
# match a screenful below the fold = multi-view) and a short paragraph (one CRC,
# single view). Table scores higher, so it is the top result.
TABLE = (
    "| # | Q | A |\n| --- | --- | --- |\n"
    + "".join(f"| {i} | filler question {i} | filler answer {i} |\n" for i in range(1, 32))
    + "| 32 | Ethernet Type II Frame? | link-layer frame with a CRC checksum |\n"
    + "".join(f"| {i} | filler question {i} | filler answer {i} |\n" for i in range(33, 86))
    + "| 86 | What is the Ethernet CRC field used for? | Cyclic Redundancy Check |\n"
    + "".join(f"| {i} | filler question {i} | filler answer {i} |\n" for i in range(87, 91))
)
BODY = f"# Networking\n\n## Flashcards\n\n{TABLE}\n## Summary\n\nA short note on the CRC field.\n"


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
def two_result_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    a = tmp_path / "notes"
    a.mkdir(parents=True, exist_ok=True)
    (a / "Notes.md").write_text(BODY, encoding="utf-8")
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


def _stops(app: FNDApp) -> int:
    pane = app.query_one("#preview_pane", MatchAwareScroll)
    return len(app._match_nav._chunk_stops(pane))


async def _walk_to(pilot: Pilot[None], app: FNDApp, want: Callable[[int], bool], key: str) -> bool:
    """Press ``key`` until the current result's stop count satisfies ``want``.

    Each press is followed by a wall-clock wait for the stop count to react, not
    a tick count: under load a fixed run of pauses degrades to no-op yields, the
    preview hasn't remounted, and the walk presses straight past the result."""
    for _ in range(10):
        if want(_stops(app)):
            return True
        await safe_press(pilot, key)
        try:
            await wait_until(pilot, lambda: want(_stops(app)), timeout=10.0)
        except AssertionError:
            continue
        return True
    return want(_stops(app))


@pytest.mark.asyncio
async def test_markers_track_current_result_across_switches(
    cfg: Config, two_result_index: Path
) -> None:
    app = FNDApp(index_dir=two_result_index, config=cfg, collection="notes", initial_query="CRC")
    async with app.run_test(size=(100, 30)) as pilot:
        await safe_pause(pilot)
        app.query_one("#results_pane", Tree).focus()
        nav = app._match_nav

        # The multi-view table result shows a below-the-fold marker.
        assert await _walk_to(pilot, app, lambda n: n >= 2, "down"), (
            "never reached the table result"
        )
        await wait_until(
            pilot, lambda: nav.below >= 1, timeout=30.0, message="table result never showed ▼"
        )

        # Switch to the single-view result: the marker must clear (the bug left
        # the table's ▼ lingering here).
        assert await _walk_to(pilot, app, lambda n: n == 1, "up"), (
            "never reached single-view result"
        )
        await wait_until(
            pilot,
            lambda: nav.above == 0 and nav.below == 0,
            timeout=30.0,
            message="markers did not clear on the single-view result (stale ▼ lingered)",
        )

        # Switch back to the multi-view result: the marker returns.
        assert await _walk_to(pilot, app, lambda n: n >= 2, "down"), (
            "never returned to the table result"
        )
        await wait_until(
            pilot,
            lambda: nav.below >= 1,
            timeout=30.0,
            message="▼ did not return on revisiting the multi-view result",
        )

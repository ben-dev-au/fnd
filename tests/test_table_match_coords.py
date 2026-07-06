"""Every matching cell of a table is registered, not just the first.

Backs preview match navigation (n/b): a big flashcards/glossary table can
hold several matches in different rows; the DataTable must expose all of
them as coordinates so the navigator can hop between them.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.coordinate import Coordinate
from textual.widgets import DataTable

from fnd.matching import MatchSpec
from fnd.tui.widgets.markdown import FNDMarkdown

TABLE_MD = (
    "| # | Q | A |\n"
    "| --- | --- | --- |\n"
    "| 32 | Ethernet Type II Frame? | link-layer frame with a CRC checksum |\n"
    "| 47 | What is the Ethernet CRC field? | Cyclic Redundancy Check |\n"
)


@pytest.mark.asyncio
async def test_all_crc_cells_are_registered() -> None:
    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield FNDMarkdown(TABLE_MD, match_spec=MatchSpec.from_query("CRC"))

    async with _Harness().run_test() as pilot:
        md = pilot.app.query_one(FNDMarkdown)
        await md.build_done.wait()
        await pilot.pause()
        dt = pilot.app.query_one(DataTable)
        coords = list(getattr(dt, "_fnd_match_coords", []))
        # Two CRC cells: card 32 answer (row 1, col 2) and card 47 question
        # (row 2, col 1).
        assert len(coords) == 2
        assert getattr(dt, "_fnd_match_coord", None) == coords[0]


# A header cell AND the first data row's SAME column both match — both map to
# (0, col) (the header has no cursor coordinate of its own), so without dedup
# the coordinate would appear twice: inflating the count and making n/b land on
# the same cell twice.
HEADER_ROW_COLLISION_MD = (
    "| CRC | Q | A |\n| --- | --- | --- |\n| CRC value here | x | y |\n| z | w | v |\n"
)


@pytest.mark.asyncio
async def test_header_and_first_row_hit_dedupe_to_one_coord() -> None:
    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield FNDMarkdown(HEADER_ROW_COLLISION_MD, match_spec=MatchSpec.from_query("CRC"))

    async with _Harness().run_test() as pilot:
        md = pilot.app.query_one(FNDMarkdown)
        await md.build_done.wait()
        await pilot.pause()
        dt = pilot.app.query_one(DataTable)
        coords = list(getattr(dt, "_fnd_match_coords", []))
        # Header col 0 and data-row-0 col 0 both match → a single (0, 0) stop.
        assert coords.count(Coordinate(0, 0)) == 1
        assert len(coords) == 1

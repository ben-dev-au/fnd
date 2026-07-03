"""Every matching cell of a table is registered, not just the first.

Backs preview match navigation (n/b): a big flashcards/glossary table can
hold several matches in different rows; the DataTable must expose all of
them as coordinates so the navigator can hop between them.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
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

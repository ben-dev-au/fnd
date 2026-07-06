"""FNDMarkdown exposes every match-bearing block in document order.

Backs preview match navigation (n/b): the navigator needs each matched
block of a chunk as a stop, not only the first. ``first_match_block`` stays
the first entry (unchanged reveal behaviour).
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from fnd.matching import MatchSpec
from fnd.tui.widgets.markdown import FNDMarkdown

MD = "# CRC intro\n\nfirst para has CRC.\n\nunrelated para.\n\nsecond CRC para.\n"


@pytest.mark.asyncio
async def test_match_blocks_lists_every_match_block() -> None:
    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield FNDMarkdown(MD, match_spec=MatchSpec.from_query("CRC"))

    async with _Harness().run_test() as pilot:
        md = pilot.app.query_one(FNDMarkdown)
        await md.build_done.wait()
        await pilot.pause()
        # heading + two matching paragraphs (the "unrelated" para is skipped)
        assert len(md.match_blocks) == 3
        assert md.match_blocks[0] is md.first_match_block
        # ordered: the heading comes before both paragraphs
        assert "CRC intro" in md.match_blocks[0]._content.plain


@pytest.mark.asyncio
async def test_match_blocks_reset_on_update() -> None:
    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield FNDMarkdown(match_spec=MatchSpec.from_query("CRC"))

    async with _Harness().run_test() as pilot:
        md = pilot.app.query_one(FNDMarkdown)
        await md.update(MD)
        await md.build_done.wait()
        await pilot.pause()
        assert len(md.match_blocks) == 3
        await md.update("# plain\n\nno query word here.\n")
        await md.build_done.wait()
        await pilot.pause()
        assert md.match_blocks == []

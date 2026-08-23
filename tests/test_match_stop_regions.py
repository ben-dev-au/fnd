"""enumerate_stop_regions finds every match stop across mounted chunks,
including cells far below the fold of a table taller than the viewport."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll

from fnd.matching import MatchSpec
from fnd.tui.preview_scroll import enumerate_stop_regions
from fnd.tui.widgets.markdown import FNDMarkdown

# A table ~4x the viewport height with CRC in row 32's answer and row 47's
# question — far apart, second cell well below the fold.
TABLE_MD = (
    "| # | Q | A |\n| --- | --- | --- |\n"
    + "".join(
        f"| {i} | filler question number {i} | filler answer number {i} |\n" for i in range(1, 32)
    )
    + "| 32 | Ethernet Type II Frame? | link-layer frame with a CRC checksum |\n"
    + "".join(
        f"| {i} | filler question number {i} | filler answer number {i} |\n" for i in range(33, 47)
    )
    + "| 47 | What is the Ethernet CRC field used for? | Cyclic Redundancy Check |\n"
    + "".join(
        f"| {i} | filler question number {i} | filler answer number {i} |\n" for i in range(48, 51)
    )
)


@pytest.mark.asyncio
async def test_two_stops_at_distinct_y() -> None:
    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield VerticalScroll(id="preview_pane")

        async def on_mount(self) -> None:
            pane = self.query_one("#preview_pane", VerticalScroll)
            await pane.mount(FNDMarkdown(TABLE_MD, match_spec=MatchSpec.from_query("CRC")))

    async with _Harness().run_test(size=(120, 25)) as pilot:
        md = pilot.app.query_one(FNDMarkdown)
        await md.build_done.wait()
        await pilot.pause()
        pane = pilot.app.query_one("#preview_pane", VerticalScroll)
        regions = enumerate_stop_regions(pane, MatchSpec.from_query("CRC"))
        ys = [r.y for r in regions]
        # both CRC cells enumerated, card 32 above card 47, distinct positions
        assert len(regions) == 2, ys
        assert ys[0] < ys[1]


# One source line wrapping over several screenfuls, its match late in it — a PDF
# contents page's shape, and the case where a block's top is nowhere near it.
WRAPPED_MD = "# Contents\n\n" + " ".join(
    f"Step {i:02d} - a deployment guide entry dotted to a page number" for i in range(90)
).replace("Step 70", "Step 70 quartzfin")


@pytest.mark.asyncio
async def test_a_wrapped_block_stops_on_the_row_its_match_paints_on() -> None:
    """A stop on the block's TOP row makes n/b jump to a row with no match on
    it, and makes the ▲▼ markers report a match off-screen while it is in view —
    both read this enumeration."""
    from textual._compositor import Compositor
    from textual.geometry import Size

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield VerticalScroll(id="preview_pane")

        async def on_mount(self) -> None:
            pane = self.query_one("#preview_pane", VerticalScroll)
            await pane.mount(FNDMarkdown(WRAPPED_MD, match_spec=MatchSpec.from_query("quartzfin")))

    async with _Harness().run_test(size=(80, 24)) as pilot:
        md = pilot.app.query_one(FNDMarkdown)
        await md.build_done.wait()
        await pilot.pause()
        block = md.first_match_block
        assert block is not None
        assert block.size.height > 20, "the fixture must wrap for this to prove anything"

        size = Size(block.size.width, max(block.size.height, block.virtual_size.height))
        comp = Compositor()
        comp.reflow(block, size)
        painted = [i for i, s in enumerate(comp.render_strips(size)) if "quartzfin" in s.text]
        assert painted, "the match never painted"

        pane = pilot.app.query_one("#preview_pane", VerticalScroll)
        regions = enumerate_stop_regions(pane, MatchSpec.from_query("quartzfin"))
        assert len(regions) == 1, [r.y for r in regions]
        assert regions[0].y == block.region.y + painted[0], (
            f"stop at y={regions[0].y} is the block's top ({block.region.y}), not the "
            f"row its match paints on ({block.region.y + painted[0]})"
        )

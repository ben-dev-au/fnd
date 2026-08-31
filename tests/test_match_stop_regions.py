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
    """A wrapped block's stop sits on its match's row, not the block's top."""
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


# One fence taller than the viewport carrying two matches far apart — the shape
# of a long code block, where every match after the first used to be unreachable.
FENCE_LINES = [f"    filler_value_{i} = {i}" for i in range(120)]
FENCE_LINES[10] = "    first = quartzfin(1)"
FENCE_LINES[90] = "    second = quartzfin(2)"
FENCE_MD = "# Code\n\n```python\n" + "\n".join(FENCE_LINES) + "\n```\n"


@pytest.mark.asyncio
async def test_a_long_fence_stops_on_every_match_it_paints() -> None:
    """One stop per block strands every match after a tall block's first."""
    from textual._compositor import Compositor
    from textual.geometry import Size

    class _Harness(App[None]):
        # Production's fence rules: under stock padding no wrap model reproduces
        # the laid-out height, so the row resolver declines and the test would
        # prove nothing (see fnd.tui.preview.match_row._rows_for_offsets).
        CSS = """
        MarkdownFence { overflow-x: hidden; padding: 0 0 0 1; }
        MarkdownFence > Label { padding: 0; width: 1fr; }
        """

        def compose(self) -> ComposeResult:
            yield VerticalScroll(id="preview_pane")

        async def on_mount(self) -> None:
            pane = self.query_one("#preview_pane", VerticalScroll)
            await pane.mount(FNDMarkdown(FENCE_MD, match_spec=MatchSpec.from_query("quartzfin")))

    async with _Harness().run_test(size=(100, 24)) as pilot:
        md = pilot.app.query_one(FNDMarkdown)
        await md.build_done.wait()
        await pilot.pause()
        block = md.first_match_block
        assert block is not None
        assert block.size.height > 24, "the fixture must exceed the viewport"

        size = Size(block.size.width, max(block.size.height, block.virtual_size.height))
        comp = Compositor()
        comp.reflow(block, size)
        painted = [i for i, s in enumerate(comp.render_strips(size)) if "quartzfin" in s.text]
        assert len(painted) == 2, f"the fixture painted {len(painted)} matches"

        pane = pilot.app.query_one("#preview_pane", VerticalScroll)
        regions = enumerate_stop_regions(pane, MatchSpec.from_query("quartzfin"))
        assert [r.y - block.region.y for r in regions] == painted


# A tab-indented fence: every line fits the pane as written, and every line
# wraps once its leading tab expands to 8 cells. Java/Go source, and the shape
# that tells a tab-aware row model from a tab-blind one.
TAB_LINES = [f"\tint filler_variable_{i:02d} = compute(one, two, three);" for i in range(60)]
TAB_LINES[8] = "\tint quartzfin_08 = compute(one, two, three, four);"
TAB_LINES[47] = "\tint quartzfin_47 = compute(one, two, three, four);"
TAB_FENCE_MD = "# Code\n\n```java\n" + "\n".join(TAB_LINES) + "\n```\n"


@pytest.mark.asyncio
async def test_a_tab_indented_fence_stops_on_every_match_it_paints() -> None:
    """Textual expands tabs to 8 cells before wrapping, so a row model that
    does not predicts the wrong height, declines, and sends every match in the
    fence to the block's top."""
    from textual._compositor import Compositor
    from textual.geometry import Size

    class _Harness(App[None]):
        CSS = """
        MarkdownFence { overflow-x: hidden; padding: 0 0 0 1; }
        MarkdownFence > Label { padding: 0; width: 1fr; }
        """

        def compose(self) -> ComposeResult:
            yield VerticalScroll(id="preview_pane")

        async def on_mount(self) -> None:
            pane = self.query_one("#preview_pane", VerticalScroll)
            await pane.mount(
                FNDMarkdown(TAB_FENCE_MD, match_spec=MatchSpec.from_query("quartzfin"))
            )

    async with _Harness().run_test(size=(64, 24)) as pilot:
        md = pilot.app.query_one(FNDMarkdown)
        await md.build_done.wait()
        await pilot.pause()
        block = md.first_match_block
        assert block is not None
        # The fixture only discriminates while both hold: nothing wraps as
        # written, and everything wraps once the tabs expand.
        assert max(len(line) for line in TAB_LINES) < block.content_region.width
        assert block.size.height == 2 * len(TAB_LINES), block.size.height

        size = Size(block.size.width, max(block.size.height, block.virtual_size.height))
        comp = Compositor()
        comp.reflow(block, size)
        painted = [i for i, s in enumerate(comp.render_strips(size)) if "quartzfin" in s.text]
        assert len(painted) == 2, f"the fixture painted {len(painted)} matches"

        pane = pilot.app.query_one("#preview_pane", VerticalScroll)
        regions = enumerate_stop_regions(pane, MatchSpec.from_query("quartzfin"))
        assert [r.y - block.region.y for r in regions] == painted

"""Freezing a chunk must keep what it painted, and cost one widget.

A chunk is tens of widgets (42 on a measured real PDF) and Textual's arrange is
linear in widget count, so mounted DOM is what makes a long reading session
progressively slower. Freezing keeps the rendered result and drops the tree.

Fidelity is not re-implemented here — the strips are the real widget tree's own
output — so the tests that matter are that the capture is COMPLETE (the whole
chunk, not just the part on screen), that the positions navigation needs survive
the widgets that produced them, and that the stand-in occupies exactly the same
height so swapping it in moves nothing.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.containers import Container, VerticalScroll
from textual.geometry import Size
from textual.widgets import DataTable

from fnd.matching import MatchSpec
from fnd.tui.preview.frozen import FrozenChunkView, freeze
from fnd.tui.widgets.markdown import FNDMarkdown
from tests._pilot_wait import wait_stable, wait_until

DOC = """## Heading with quartzfin

A paragraph mentioning quartzfin in prose, long enough to wrap across more than
one row so the capture has something non-trivial to hold.

| Option | Notes |
| --- | --- |
| `alpha` | a **quartzfin** cell |
| beta | plain |

```python
def sample():
    return "quartzfin in a fence"
```

- list item with quartzfin

""" + "\n\n".join(
    f"Filler paragraph {i}, present so the chunk is comfortably taller than the "
    f"test viewport — otherwise 'captures past the fold' proves nothing."
    for i in range(12)
)


class _Host(App[None]):
    def compose(self) -> ComposeResult:
        with VerticalScroll(id="pane"):
            yield FNDMarkdown(match_spec=MatchSpec.from_query("quartzfin"), id="md")


async def _built(pilot) -> FNDMarkdown:  # type: ignore[no-untyped-def]
    md = pilot.app.query_one("#md", FNDMarkdown)
    md.update(DOC)
    await md.build_done.wait()
    # Geometry, not just the build: freeze needs a laid-out tree, and
    # `build_done` is not a layout signal. A fixed tick count is a wait only
    # while the machine is idle and degrades to a no-op under suite load.
    #
    # The TABLE's geometry, not just the chunk's. A DataTable sizes itself in
    # response to its own posted refresh, so the chunk can report a height while
    # the table inside it is still rows-with-no-geometry — precisely what freeze
    # refuses. Waiting on the chunk alone let CI reach the capture first and see
    # it declined, on both macOS and Windows.
    await wait_until(
        pilot,
        lambda: (
            md.size.height > 0
            and md.virtual_size.height > 0
            and all(dt.size.height > 0 for dt in md.query(DataTable))
        ),
        timeout=15.0,
        message="the chunk never laid out",
    )
    return md


@pytest.mark.asyncio
async def test_capture_holds_everything_the_tree_painted() -> None:
    app = _Host()
    async with app.run_test(size=(90, 24)) as pilot:
        md = await _built(pilot)
        frozen = freeze(md, chunk_seq=7)
        assert frozen is not None, "a laid-out chunk must be capturable"

        text = "\n".join(s.text for s in frozen.strips)
        # Every construct, including the ones a re-implemented renderer loses.
        assert "quartzfin in prose" in text.replace("\n", " ")
        # ONE line carrying both words. "quartzfin" alone is in the heading,
        # the prose, the fence and the list item, so asserting it document-wide
        # passes even when the table captures as an empty box — the exact
        # failure the table guards exist to catch.
        assert any("quartzfin" in line and "cell" in line for line in text.splitlines()), (
            "the table row did not survive the capture"
        )
        assert any(c in text for c in "─│"), "table borders missing"
        assert "quartzfin in a fence" in text, "fenced code missing"
        assert "list item with quartzfin" in text, "list item missing"

        # The capture must cover the WHOLE chunk, not the part on screen: the
        # pane here is 24 rows and the chunk is taller.
        assert frozen.height >= md.virtual_size.height, (
            f"captured {frozen.height} rows for a chunk {md.virtual_size.height} tall"
        )
        assert frozen.height > 24, "chunk should exceed the viewport for this to prove anything"

        styled = sum(
            1 for s in frozen.strips for seg in s._segments if seg.style and seg.style.bgcolor
        )
        assert styled, "match highlighting did not survive the capture"


@pytest.mark.asyncio
async def test_positions_survive_the_widgets_that_produced_them() -> None:
    app = _Host()
    async with app.run_test(size=(90, 24)) as pilot:
        md = await _built(pilot)
        live_first = md.first_match_block
        assert live_first is not None
        live_row = live_first.region.y - md.region.y

        frozen = freeze(md, chunk_seq=7)
        assert frozen is not None
        assert frozen.first_match_row == live_row, (
            f"first match row {frozen.first_match_row} != live {live_row}"
        )
        assert frozen.stop_rows, "no match stops captured"
        assert all(0 <= r < frozen.height for r in frozen.stop_rows)
        # Table cells resolve to rows recorded while the DataTable still existed —
        # the point being that a row cannot race, whereas a live cell region can.
        for coord, row in frozen.cell_rows.items():
            assert 0 <= row < frozen.height, f"cell {coord} row {row} outside the chunk"


@pytest.mark.asyncio
async def test_the_stand_in_is_one_widget_of_the_same_height() -> None:
    app = _Host()
    async with app.run_test(size=(90, 24)) as pilot:
        md = await _built(pilot)
        tree_widgets = len(list(md.query("*")))
        assert tree_widgets > 1, "fixture should build a real tree"
        frozen = freeze(md, chunk_seq=7)
        assert frozen is not None

        pane = app.query_one("#pane", VerticalScroll)
        view = FrozenChunkView(frozen)
        await pane.mount(view)
        await wait_until(
            pilot,
            lambda: view.size.height > 0,
            timeout=15.0,
            message="the stand-in never laid out",
        )

        assert len(list(view.query("*"))) == 0, "the stand-in must hold no child widgets"
        assert view.size.height == frozen.height, (
            f"stand-in is {view.size.height} rows, capture is {frozen.height} — "
            "a height mismatch would shift the page when it is swapped in"
        )
        rendered = "\n".join(view.render_line(y).text for y in range(view.size.height))
        assert "quartzfin in a fence" in rendered
        assert "cell" in rendered


class _PaddedHost(App[None]):
    """A chunk as the preview actually mounts it — with the padding classes.

    ``_Host`` deliberately has none, so it cannot see this: ``freeze`` captures
    the CONTENT region, and a stand-in sized to that alone is a row shorter than
    the padded widget it replaces.
    """

    CSS = """
    #pane { height: 100%; }
    #body { height: auto; }
    .chunk-section { padding: 0 0 1 0; height: auto; }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="pane"), Container(id="body"):
            for i in range(4):
                yield FNDMarkdown(
                    match_spec=MatchSpec.from_query("quartzfin"),
                    id=f"c{i}",
                    classes="chunk-section chunk-md-body",
                )


@pytest.mark.asyncio
async def test_freezing_above_the_viewport_does_not_move_the_page() -> None:
    """The sweep freezes chunks ABOVE the viewport as well as below.

    If a stand-in is even one row shorter than the chunk it replaces, the
    content above the viewport shrinks while ``scroll_y`` stays put, and
    everything the user is reading slides upward — a second or two after the
    navigation landed, which is exactly when it reads as the page jumping on its
    own. Measured before the padding was carried across: -6 rows for 6 chunks.
    """
    app = _PaddedHost()
    async with app.run_test(size=(90, 24)) as pilot:
        for i in range(4):
            md = app.query_one(f"#c{i}", FNDMarkdown)
            md.update(DOC)
            await md.build_done.wait()
        await wait_until(
            pilot,
            lambda: all(app.query_one(f"#c{i}", FNDMarkdown).size.height > 0 for i in range(4)),
            timeout=15.0,
            message="the fixture chunks never laid out",
        )

        above = [app.query_one(f"#c{i}", FNDMarkdown) for i in range(3)]
        assert all(w.outer_size.height > w.size.height for w in above), (
            "fixture must actually have padding, or this proves nothing"
        )

        pane = app.query_one("#pane", VerticalScroll)
        reading = app.query_one("#c3", FNDMarkdown)
        pane.scroll_to(y=reading.virtual_region.y, animate=False)
        # Stability, not a predicted position. Two earlier attempts guessed and
        # both were wrong: `scroll_y == scroll_target_y` settles before the
        # compositor re-arranges and read a position 174 rows stale, and
        # `reading.region.y == pane.region.y` assumes the scroll lands exactly at
        # the chunk top — it can be clamped short, and the wait then times out
        # under load while passing on an idle machine.
        #
        # The assertion below needs only that the position has STOPPED moving.
        # `before` is whatever it settled at; it never had to be 0.
        await wait_stable(
            pilot,
            lambda: (pane.scroll_y, reading.region.y, reading.region.height),
            rounds=3,
            timeout=15.0,
            message="the scroll never settled",
        )
        assert reading.region.height > 0, "the chunk being read is not on screen"
        before = reading.region.y - pane.region.y

        for i, md in enumerate(above):
            captured = freeze(md, chunk_seq=i)
            assert captured is not None
            md.parent.mount(FrozenChunkView(captured), before=md)  # type: ignore[union-attr]
            md.remove()
        await wait_until(
            pilot,
            lambda: (
                len(list(pane.query(FrozenChunkView))) == len(above)
                and len(list(pane.query(FNDMarkdown))) == 1
            ),
            timeout=15.0,
            message="the stand-ins never replaced the chunks above",
        )
        # And let the swap's own re-layout settle, for the same reason.
        await wait_stable(
            pilot,
            lambda: (pane.scroll_y, reading.region.y),
            rounds=3,
            timeout=15.0,
            message="the layout never settled after the swap",
        )

        after = reading.region.y - pane.region.y
        assert after == before, (
            f"the page moved {after - before:+d} rows when {len(above)} chunks above it "
            "were frozen — the stand-ins are not the height of what they replaced"
        )


@pytest.mark.asyncio
async def test_a_chunk_that_scrolls_inside_itself_is_refused() -> None:
    """The one thing a flat run of strips cannot represent. Nothing should hit
    this now that tables lay out in full, but the guard is what makes freezing
    safe in general, so it is pinned rather than assumed unreachable."""
    app = _Host()
    async with app.run_test(size=(90, 24)) as pilot:
        md = await _built(pilot)
        dt = next(iter(md.query(DataTable)), None)
        assert dt is not None, "fixture should render a DataTable"
        dt.styles.max_height = 3  # force it to become a nested viewport
        dt.refresh(layout=True)
        await wait_until(
            pilot,
            lambda: dt.virtual_size.height > dt.size.height,
            timeout=15.0,
            message="failed to induce a nested scroll",
        )
        assert dt.virtual_size.height > dt.size.height, "failed to induce a nested scroll"
        assert freeze(md, chunk_seq=7) is None, "a nested scroll region must not be flattened"


@pytest.mark.asyncio
async def test_a_frozen_chunk_still_contributes_its_match_stops() -> None:
    """Freezing must not make matches unreachable.

    ``enumerate_stop_regions`` walks ``FNDMarkdown`` blocks, and a frozen chunk
    is not one — so without explicit handling it contributes nothing and its
    matches drop out of n/b navigation and the off-screen markers. Nothing
    raises; the matches simply stop existing, which is why this is pinned.
    """
    from fnd.tui.preview_scroll import enumerate_stop_regions

    app = _Host()
    async with app.run_test(size=(90, 24)) as pilot:
        md = await _built(pilot)
        pane = app.query_one("#pane", VerticalScroll)
        spec = md.match_spec

        live_stops = len(enumerate_stop_regions(pane, spec))
        assert live_stops > 0, "fixture should have match stops while live"

        frozen = freeze(md, chunk_seq=7)
        assert frozen is not None
        view = FrozenChunkView(frozen)
        await pane.mount(view)
        await md.remove()
        await wait_until(
            pilot,
            lambda: not list(pane.query(FNDMarkdown)) and view.size.height > 0,
            timeout=15.0,
            message="the live chunk never went away",
        )

        frozen_stops = len(enumerate_stop_regions(pane, spec))
        assert frozen_stops == live_stops, (
            f"{live_stops} stops live but {frozen_stops} once frozen — "
            "the chunk's matches became unreachable by n/b and the markers"
        )


@pytest.mark.asyncio
async def test_a_table_that_has_not_laid_out_is_refused() -> None:
    """An unlaid table captures as a BORDER with no contents — an empty box, and
    downstream nothing can tell it from a table that is genuinely empty.

    The nested-scroll guard cannot see this: an unlaid table measures 0 both
    ways, so ``virtual > size`` is ``0 > 0`` and passes. Measured off-screen,
    where layout has to be driven by hand: rows=3, size=0, virtual=0, and the
    capture came back holding the border and none of the cells.
    """
    app = _Host()
    async with app.run_test(size=(90, 24)) as pilot:
        md = await _built(pilot)
        dt = next(iter(md.query(DataTable)), None)
        assert dt is not None, "fixture should render a DataTable"
        assert dt.row_count > 0
        assert freeze(md, chunk_seq=7) is not None, "a laid-out table must be capturable"

        # Exactly the state an off-screen build is in before the message pump
        # has run: rows present, geometry not yet assigned.
        dt._size = Size(0, 0)  # type: ignore[attr-defined]
        dt.virtual_size = Size(0, 0)
        assert freeze(md, chunk_seq=7) is None, (
            "a table with rows but no geometry was captured — it would be served "
            "as an empty box where the table should be"
        )

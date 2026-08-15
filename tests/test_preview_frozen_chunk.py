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
from textual.containers import VerticalScroll
from textual.widgets import DataTable

from fnd.matching import MatchSpec
from fnd.tui.preview.frozen import FrozenChunkView, freeze
from fnd.tui.widgets.markdown import FNDMarkdown

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
    for _ in range(12):
        await pilot.pause()
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
        assert "quartzfin" in text, "table cell match text missing"
        assert "cell" in text, "table cell text missing"
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
        for _ in range(6):
            await pilot.pause()

        assert len(list(view.query("*"))) == 0, "the stand-in must hold no child widgets"
        assert view.size.height == frozen.height, (
            f"stand-in is {view.size.height} rows, capture is {frozen.height} — "
            "a height mismatch would shift the page when it is swapped in"
        )
        rendered = "\n".join(view.render_line(y).text for y in range(view.size.height))
        assert "quartzfin in a fence" in rendered
        assert "cell" in rendered


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
        for _ in range(10):
            await pilot.pause()
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
        for _ in range(8):
            await pilot.pause()

        frozen_stops = len(enumerate_stop_regions(pane, spec))
        assert frozen_stops == live_stops, (
            f"{live_stops} stops live but {frozen_stops} once frozen — "
            "the chunk's matches became unreachable by n/b and the markers"
        )

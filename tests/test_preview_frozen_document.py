"""A whole file as one widget: assembly, navigation, and invisible prepending.

The document view exists for a reason beyond widget count. Adding content ABOVE
the viewport is what makes warming a file visible, and only a widget that owns
its ``virtual_size`` can grow and scroll atomically. A container's virtual size
is assigned BY the layout pass, so its compensating scroll is always validated
against a stale extent — measured, a 7-row error, or three frames of drift when
corrected afterwards.

So the tests that matter are: the assembly is lossless, a jump lands on the
match, and prepending does not move the view.
"""

from __future__ import annotations

import pytest
from rich.segment import Segment
from textual.app import App, ComposeResult
from textual.strip import Strip

from fnd.tui.preview.frozen import FrozenChunk, FrozenDocument, FrozenDocumentView

WIDTH = 40
VIEWPORT = 16


def _chunk(
    seq: int,
    rows: int,
    *,
    match_row: int | None = None,
    stops: list[int] | None = None,
    cells: dict[tuple[int, int], int] | None = None,
) -> FrozenChunk:
    return FrozenChunk(
        chunk_seq=seq,
        width=WIDTH,
        strips=[Strip([Segment(f"c{seq}r{r}".ljust(WIDTH))], WIDTH) for r in range(rows)],
        first_match_row=match_row,
        stop_rows=stops or [],
        cell_rows=cells or {},
    )


def _text(doc: FrozenDocument, row: int) -> str:
    strip = doc.line(row)
    assert strip is not None, f"row {row} has no line"
    return strip.text.strip()


class _Host(App[None]):
    def __init__(self, document: FrozenDocument) -> None:
        super().__init__()
        self._document = document

    def compose(self) -> ComposeResult:
        yield FrozenDocumentView(self._document, id="doc")


def test_assembly_maps_every_row_to_the_right_chunk() -> None:
    doc = FrozenDocument()
    for seq, rows in ((10, 4), (20, 7), (30, 3)):
        doc.append(_chunk(seq, rows))
    assert doc.total_rows == 14
    assert [_text(doc, r) for r in range(4)] == ["c10r0", "c10r1", "c10r2", "c10r3"]
    assert _text(doc, 4) == "c20r0"
    assert _text(doc, 13) == "c30r2"
    assert doc.line(14) is None
    assert doc.chunk_at_row(0) == 10
    assert doc.chunk_at_row(4) == 20
    assert doc.chunk_at_row(13) == 30


def test_positions_are_document_relative() -> None:
    doc = FrozenDocument()
    doc.append(_chunk(10, 4))
    doc.append(_chunk(20, 7, match_row=2, stops=[2, 5], cells={(1, 0): 3}))
    # Offsets recorded inside a chunk become document rows once assembled.
    assert doc.match_row(20) == 4 + 2
    assert doc.cell_row(20, (1, 0)) == 4 + 3
    assert doc.stop_rows() == [6, 9]
    assert doc.match_row(99) is None
    assert doc.cell_row(20, (9, 9)) is None


def test_prepending_shifts_every_later_position() -> None:
    """A prepend must move the rows of everything after it, or a jump computed
    before the prepend would land somewhere else afterwards."""
    doc = FrozenDocument()
    doc.append(_chunk(20, 5, match_row=1))
    assert doc.match_row(20) == 1
    doc.prepend(_chunk(10, 6, match_row=0))
    assert doc.match_row(20) == 6 + 1
    assert doc.match_row(10) == 0
    assert doc.total_rows == 11
    assert doc.chunk_at_row(0) == 10
    assert doc.chunk_at_row(6) == 20


@pytest.mark.asyncio
async def test_a_jump_lands_with_the_match_on_screen() -> None:
    doc = FrozenDocument()
    for seq in range(6):
        doc.append(_chunk(seq, 30, match_row=17))
    app = _Host(doc)
    async with app.run_test(size=(WIDTH + 2, VIEWPORT)) as pilot:
        view = app.query_one("#doc", FrozenDocumentView)
        for _ in range(4):
            await pilot.pause()
        # Forwards and backwards: the backward jump is the case that rebuilds in
        # the widget model.
        for seq in [*range(6), *reversed(range(6))]:
            assert view.scroll_to_chunk(seq)
            await pilot.pause()
            row = doc.match_row(seq)
            top = int(view.scroll_offset.y)
            assert row is not None
            assert top <= row < top + view.size.height, (
                f"chunk {seq}: match row {row} outside viewport [{top}, {top + view.size.height})"
            )


@pytest.mark.asyncio
async def test_prepending_does_not_move_the_viewport() -> None:
    """The whole reason this is one widget.

    ``virtual_size`` is set before the scroll; reversed, ``validate_scroll_y``
    clamps against the old extent and the view drifts by exactly the clamp.
    """
    doc = FrozenDocument()
    for seq in range(4):
        doc.append(_chunk(seq, 20))
    app = _Host(doc)
    async with app.run_test(size=(WIDTH + 2, VIEWPORT)) as pilot:
        view = app.query_one("#doc", FrozenDocumentView)
        for _ in range(4):
            await pilot.pause()
        # Scroll to the BOTTOM of the current extent. The clamp only bites when
        # the compensating scroll would exceed the pre-growth max_scroll_y, so a
        # comfortable mid-document position proves nothing — an earlier version
        # of this test sat at row 50 of 80 and passed with the ordering reversed.
        view.scroll_to_row(view.virtual_size.height, context_fraction=0.0)
        for _ in range(4):
            await pilot.pause()
        assert int(view.scroll_offset.y) == view.virtual_size.height - view.size.height, (
            "expected to be pinned at the bottom, where the clamp applies"
        )

        before = view.render_line(0).text.rstrip()
        seen: list[str] = []
        for seq in range(100, 106):
            view.prepend(_chunk(seq, 9))
            for _ in range(3):
                await pilot.pause()
                seen.append(view.render_line(0).text.rstrip())

        assert sorted(set(seen)) == [before], (
            f"viewport moved while prepending: saw {sorted(set(seen))!r}, expected only {before!r}"
        )
        assert view.virtual_size.height == doc.total_rows


@pytest.mark.asyncio
async def test_appending_never_moves_the_viewport() -> None:
    doc = FrozenDocument()
    for seq in range(4):
        doc.append(_chunk(seq, 20))
    app = _Host(doc)
    async with app.run_test(size=(WIDTH + 2, VIEWPORT)) as pilot:
        view = app.query_one("#doc", FrozenDocumentView)
        for _ in range(4):
            await pilot.pause()
        view.scroll_to_row(40, context_fraction=0.0)
        for _ in range(4):
            await pilot.pause()
        before = view.render_line(0).text.rstrip()
        for seq in range(200, 204):
            view.append(_chunk(seq, 11))
            for _ in range(2):
                await pilot.pause()
        assert view.render_line(0).text.rstrip() == before
        assert view.virtual_size.height == doc.total_rows

"""Where a backward hand-over enters a section: its LAST match, by painted row.

The ordering trap this exists for: ``FNDMarkdown.match_blocks`` is registration
order, and a table registers from ``compose`` (at mount) while every text block
registers pre-mount — so the table is always last, whatever follows it in the
document. The rows are the only ordering that matches what the reader sees.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import DataTable

from fnd.matching import MatchSpec
from fnd.tui.preview.match_row import chunk_stop_rows
from fnd.tui.widgets.markdown import FNDMarkdown, FNDMarkdownTableDT

# A matching table, then filler, then a matching paragraph well below it.
TABLE_THEN_TAIL = (
    "# Intro\n\n"
    "| # | Q | A |\n| --- | --- | --- |\n"
    "| 32 | Ethernet frame | carries a CRC checksum |\n"
    "| 47 | Padding | not relevant |\n\n"
    + "".join(f"Filler paragraph {i} with nothing to find.\n\n" for i in range(12))
    + "The tail paragraph mentions CRC last.\n"
)


class _Harness(App[None]):
    def compose(self) -> ComposeResult:
        yield FNDMarkdown(TABLE_THEN_TAIL, match_spec=MatchSpec.from_query("CRC"))


@pytest.mark.asyncio
async def test_the_last_stop_row_is_the_tail_paragraph_not_the_table() -> None:
    async with _Harness().run_test(size=(80, 24)) as pilot:
        md = pilot.app.query_one(FNDMarkdown)
        await md.build_done.wait()
        await pilot.pause()
        spec = MatchSpec.from_query("CRC")

        rows, cells = chunk_stop_rows(md, spec)

        assert rows, "the chunk painted no match rows"
        assert cells, "the table's matching cell was not resolved"
        table = pilot.app.query_one(FNDMarkdownTableDT)
        table_row = table.region.y - md.region.y
        assert rows[-1] > table_row, (
            f"the last stop {rows[-1]} is at or above the table at row {table_row} — "
            "match_blocks ordering, not painted order"
        )
        assert rows[-1] == max(rows), "rows are not sorted"


@pytest.mark.asyncio
async def test_the_table_cell_is_one_of_the_stops() -> None:
    """The table is not skipped for being out of registration order — its cell
    is a stop like any other, just not the last one here."""
    async with _Harness().run_test(size=(80, 24)) as pilot:
        md = pilot.app.query_one(FNDMarkdown)
        await md.build_done.wait()
        await pilot.pause()
        dt = pilot.app.query_one(DataTable)
        coords = list(getattr(dt, "_fnd_match_coords", []))
        assert coords, "the fixture's table has no matching cell"

        _rows, cells = chunk_stop_rows(md, MatchSpec.from_query("CRC"))

        assert (coords[0].row, coords[0].column) in cells


class _FakePane:
    """Records the region the strategy asks for, without a real scroll."""

    def __init__(self, height: int = 24) -> None:
        from textual.geometry import Offset, Region, Size

        self.size = Size(80, height)
        self.scroll_offset = Offset(0, 0)
        self.scrollable_content_region = Region(0, 0, 80, height)
        self.virtual_size = Size(80, 10**6)
        self.captured: object = None

    @property
    def max_scroll_y(self) -> int:
        return 10**6

    def scroll_to_region(self, region: object, **_kw: object) -> None:
        self.captured = region


class _FakeHost:
    def __init__(self, pane: _FakePane, chunk: object) -> None:
        self._pane = pane
        self._chunk_widgets = {5: chunk}
        self.deferred: list[tuple[object, tuple[object, ...]]] = []

    def preview_pane(self) -> _FakePane:
        return self._pane

    def effective_match_spec(self) -> MatchSpec:
        return MatchSpec.from_query("CRC")

    def begin_reconcile_scroll(self) -> None: ...

    def end_reconcile_scroll(self) -> None: ...

    def swap_reveal_target(self, target: object, margin: int, anchor_region: object = None) -> bool:
        return False

    def call_after_refresh(self, callback: object, *args: object, **kwargs: object) -> None:
        self.deferred.append((callback, args))

    def above_window_pending(self, focus_chunk_seq: int) -> bool:
        return False

    def pipeline_busy(self) -> bool:
        return False

    def diag_log(self, msg: str) -> None: ...

    @property
    def chunk_widgets(self) -> dict[int, object]:
        return self._chunk_widgets

    @property
    def match_targets(self) -> dict[int, object]:
        return {}


@pytest.mark.asyncio
async def test_a_last_match_landing_reaches_the_tail_paragraph_past_the_table() -> None:
    """The landing itself, not just the row helper: a chunk whose registration
    order ends with the table must still land on the paragraph below it."""
    from typing import cast

    from fnd.tui.preview_scroll import StructuralHost, StructuralScrollStrategy

    async with _Harness().run_test(size=(80, 24)) as pilot:
        md = pilot.app.query_one(FNDMarkdown)
        await md.build_done.wait()
        await pilot.pause()
        rows, _cells = chunk_stop_rows(md, MatchSpec.from_query("CRC"))
        table_row = pilot.app.query_one(FNDMarkdownTableDT).region.y - md.region.y
        pane = _FakePane()
        host = _FakeHost(pane, md)
        strat = StructuralScrollStrategy(cast(StructuralHost, host))

        strat._do_scroll_to_chunk(5, margin_from=0.25, intent="last_match")

        assert pane.captured is not None
        # The landing sits a quarter-viewport above the match it reveals.
        want = md.region.y + rows[-1] - int(pane.size.height * 0.25)
        assert pane.captured.y == want  # type: ignore[attr-defined]
        assert pane.captured.y > md.region.y + table_row  # type: ignore[attr-defined]


def test_a_landing_intent_serves_every_arm_of_its_own_navigation() -> None:
    """A navigation arms more than once — the flat path arms again after the
    structural one — so a read must not consume the request. It is dropped when
    a load for a DIFFERENT target starts, which is the navigation ending."""
    from fnd.tui.preview.presenter import PreviewPresenter

    presenter = PreviewPresenter.__new__(PreviewPresenter)
    presenter.pending_landing_intent = ("file-a", 7, "last_match")

    assert presenter._landing_intent("file-a", 7) == "last_match"
    assert presenter._landing_intent("file-a", 7) == "last_match", "the second arm lost it"
    assert presenter._landing_intent("file-b", 7) == "first_match"

    presenter._drop_stale_landing_intent("file-a", 7)
    assert presenter.pending_landing_intent is not None, "its own target is not stale"
    presenter._drop_stale_landing_intent("file-a", 9)
    assert presenter.pending_landing_intent is None, "a new navigation drops it"


@pytest.mark.asyncio
async def test_a_last_match_landing_waits_for_an_unresolved_table_cell() -> None:
    """A cell whose region has not laid out is missing from the stop set, so
    committing then lands on whatever precedes the table. The first-match path
    already retries for this; the backward entry has to as well."""
    from typing import cast

    from fnd.tui.preview_scroll import StructuralHost, StructuralScrollStrategy

    async with _Harness().run_test(size=(80, 24)) as pilot:
        md = pilot.app.query_one(FNDMarkdown)
        await md.build_done.wait()
        await pilot.pause()
        dt = pilot.app.query_one(DataTable)
        dt._get_cell_region = lambda _coord: (_ for _ in ()).throw(RuntimeError("not laid out"))  # type: ignore[assignment]
        pane = _FakePane()
        host = _FakeHost(pane, md)
        strat = StructuralScrollStrategy(cast(StructuralHost, host))

        strat._do_scroll_to_chunk(5, margin_from=0.25, intent="last_match")

        assert pane.captured is None, "committed a landing over an unresolved cell"
        assert host.deferred, "did not retry for the cell to lay out"

"""An outline jump ("chunk_top") lands a heading on the reading line, never a match."""

from __future__ import annotations

from typing import Any, cast

import pytest
from textual.app import App, ComposeResult

from fnd.matching import MatchSpec
from fnd.tui.preview.frozen import FrozenChunk, FrozenChunkView, freeze
from fnd.tui.preview.match_row import heading_rows
from fnd.tui.preview_scroll import (
    FlatHost,
    FlatScrollStrategy,
    ScrollAnchor,
    StructuralHost,
    StructuralScrollStrategy,
)
from fnd.tui.widgets.markdown import FNDMarkdown

# Three headings, with the only match well below the first.
PAGE = (
    "# Opening\n\n"
    + "".join(f"Filler paragraph {i}.\n\n" for i in range(6))
    + "## Middle\n\n"
    + "".join(f"More filler {i}.\n\n" for i in range(6))
    + "## Closing\n\nThe quartzfin match sits here.\n"
)
SPEC = MatchSpec.from_query("quartzfin")


class _Harness(App[None]):
    def compose(self) -> ComposeResult:
        yield FNDMarkdown(PAGE, match_spec=SPEC)


class _FakePane:
    def __init__(self, height: int = 24) -> None:
        from textual.geometry import Offset, Region, Size

        self.size = Size(80, height)
        self.scroll_offset = Offset(0, 0)
        self.scrollable_content_region = Region(0, 0, 80, height)
        self.virtual_size = Size(80, 10**6)
        self.captured: Any = None

    @property
    def max_scroll_y(self) -> int:
        return 10**6

    def scroll_to_region(self, region: object, **_kw: object) -> None:
        self.captured = region


class _FakeHost:
    def __init__(self, pane: _FakePane, chunk: object) -> None:
        self._pane = pane
        self._chunk_widgets = {5: chunk}
        self.deferred: list[object] = []

    def preview_pane(self) -> _FakePane:
        return self._pane

    def effective_match_spec(self) -> MatchSpec:
        return SPEC

    def begin_reconcile_scroll(self) -> None: ...

    def end_reconcile_scroll(self) -> None: ...

    def swap_reveal_target(self, target: object, margin: int, anchor_region: object = None) -> bool:
        return False

    def call_after_refresh(self, callback: object, *args: object, **kwargs: object) -> None:
        self.deferred.append(callback)

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


def _land(md: object, **kw: Any) -> tuple[_FakePane, _FakeHost]:
    pane = _FakePane()
    host = _FakeHost(pane, md)
    StructuralScrollStrategy(cast(StructuralHost, host))._do_scroll_to_chunk(
        5, margin_from=0.25, **kw
    )
    return pane, host


async def _built(pilot: Any) -> FNDMarkdown:
    md = pilot.app.query_one(FNDMarkdown)
    await md.build_done.wait()
    await pilot.pause()
    return md


@pytest.mark.asyncio
async def test_a_chunk_top_landing_ignores_the_match() -> None:
    async with _Harness().run_test(size=(80, 24)) as pilot:
        md = await _built(pilot)
        margin = int(24 * 0.25)

        on_match, _ = _land(md)
        on_top, _ = _land(md, intent="chunk_top")

        assert on_top.captured.y == max(0, md.region.y - margin)
        assert on_match.captured.y > on_top.captured.y, "the match landing did not move"


@pytest.mark.asyncio
async def test_a_heading_landing_puts_that_heading_on_the_reading_line() -> None:
    async with _Harness().run_test(size=(80, 24)) as pilot:
        md = await _built(pilot)
        rows = heading_rows(md)
        assert rows is not None
        assert len(rows) == 3
        assert rows[0] < rows[1] < rows[2]

        wants = [max(0, md.region.y + row - int(24 * 0.25)) for row in rows]
        assert wants[-1] > 0, "the last heading must sit clear of the clamp to prove anything"
        for ordinal, want in enumerate(wants):
            pane, _ = _land(md, intent="chunk_top", heading=ordinal)
            assert pane.captured.y == want, ordinal


@pytest.mark.asyncio
async def test_a_heading_past_the_rendered_ones_lands_on_the_chunk_top() -> None:
    async with _Harness().run_test(size=(80, 24)) as pilot:
        md = await _built(pilot)
        pane, _ = _land(md, intent="chunk_top", heading=9)
        assert pane.captured.y == max(0, md.region.y - int(24 * 0.25))


@pytest.mark.asyncio
async def test_a_heading_landing_waits_for_the_build() -> None:
    async with _Harness().run_test(size=(80, 24)) as pilot:
        md = await _built(pilot)
        md.build_done.clear()
        pane, host = _land(md, intent="chunk_top", heading=1)
        assert pane.captured is None, "committed before the headings existed"
        assert host.deferred, "did not retry"


@pytest.mark.asyncio
async def test_a_frozen_chunk_keeps_its_heading_rows() -> None:
    async with _Harness().run_test(size=(80, 24)) as pilot:
        md = await _built(pilot)
        live = heading_rows(md)
        frozen = freeze(md, 5)
        assert frozen is not None
        assert frozen.heading_rows == live
        titles = [frozen.strips[row].text for row in frozen.heading_rows]
        assert [t.strip(" #") for t in titles] == ["Opening", "Middle", "Closing"]
        view = FrozenChunkView(frozen)
        assert heading_rows(view) == live
        strat = StructuralScrollStrategy(cast(StructuralHost, _FakeHost(_FakePane(), view)))
        assert live is not None
        assert strat._heading_row(view, 2) == live[2]


def test_a_frozen_view_without_headings_lands_on_its_top() -> None:
    view = FrozenChunkView(FrozenChunk(chunk_seq=5, width=80, strips=[]))
    strat = StructuralScrollStrategy(cast(StructuralHost, _FakeHost(_FakePane(), view)))
    assert strat._heading_row(view, 0) == 0


class _FakeView:
    def __init__(self) -> None:
        self.calls: list[tuple[int, bool, float]] = []

    def scroll_to_chunk(
        self, chunk_id: int, *, prefer_first_match: bool = True, context_fraction: float = 0.0
    ) -> None:
        self.calls.append((chunk_id, prefer_first_match, context_fraction))

    def address_of_chunk(self, chunk_id: int) -> int | None:
        del chunk_id
        return None


class _FlatHost:
    def __init__(self, view: _FakeView) -> None:
        self._view = view

    def active_flat_buffer(self) -> _FakeView:
        return self._view


@pytest.mark.parametrize(("intent", "prefer"), [("first_match", True), ("chunk_top", False)])
def test_the_flat_buffer_lands_on_the_chunk_top_for_a_jump(intent: Any, prefer: bool) -> None:
    view = _FakeView()
    strat = FlatScrollStrategy(cast(FlatHost, _FlatHost(view)))
    strat.reconcile(ScrollAnchor("doc", 4, intent=intent))
    assert view.calls == [(4, prefer, 0.25)]


# ── the intent's lifetime on the presenter ───────────────────────────────


def _presenter() -> Any:
    from fnd.tui.preview.presenter import PreviewPresenter

    p = PreviewPresenter.__new__(PreviewPresenter)
    p.pending_landing_intent = None
    p.pending_heading = -1
    p.inflight_target = None
    return p


def test_a_jump_intent_carries_its_heading() -> None:
    p = _presenter()
    p.pending_landing_intent = ("doc", 3, "chunk_top")
    p.pending_heading = 2
    assert p._landing_intent("doc", 3) == "chunk_top"
    assert p._landing_heading("doc", 3) == 2
    assert p._landing_heading("doc", 4) == -1


def test_a_load_without_an_intent_drops_a_jump_to_the_same_chunk() -> None:
    p = _presenter()
    p.pending_landing_intent = ("doc", 3, "chunk_top")
    p.inflight_target = ("doc", 3)
    p._drop_stale_landing_intent("doc", 3)
    assert p.pending_landing_intent is None
    assert p.inflight_target is None, "the stale latch would swallow the load"


def test_a_backward_hand_over_survives_its_own_load() -> None:
    p = _presenter()
    p.pending_landing_intent = ("doc", 3, "last_match")
    p.inflight_target = ("doc", 3)
    p._drop_stale_landing_intent("doc", 3)
    assert p.pending_landing_intent == ("doc", 3, "last_match")
    assert p.inflight_target == ("doc", 3)


def test_a_reset_forgets_any_pending_intent() -> None:
    from unittest.mock import MagicMock

    from fnd.tui.preview.presenter import PreviewPresenter

    p = MagicMock()
    p.reset_generation = 0
    p.pending_landing_intent = ("doc", 3, "chunk_top")
    PreviewPresenter.bump_reset_generation(p)
    assert p.pending_landing_intent is None

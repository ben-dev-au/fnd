from typing import cast

from textual.geometry import Offset, Region, Size

from fnd.matching import MatchSpec
from fnd.tui.preview_scroll import (
    FlatHost,
    FlatScrollStrategy,
    PreviewScrollController,
    ScrollAnchor,
    StructuralHost,
    StructuralScrollStrategy,
)


class FakeStrategy:
    def __init__(self) -> None:
        self.calls: list[ScrollAnchor] = []

    def reconcile(self, anchor: ScrollAnchor) -> None:
        self.calls.append(anchor)


def test_arm_then_reconcile_calls_strategy() -> None:
    strat = FakeStrategy()
    c = PreviewScrollController(select_strategy=lambda: strat)
    a = ScrollAnchor(parent_id="p", focus_chunk_seq=3)
    c.arm(a)
    assert c.is_armed
    c.reconcile()
    assert strat.calls == [a]


def test_reconcile_is_noop_when_released() -> None:
    strat = FakeStrategy()
    c = PreviewScrollController(select_strategy=lambda: strat)
    c.arm(ScrollAnchor(parent_id="p", focus_chunk_seq=0))
    c.release()
    assert not c.is_armed
    c.reconcile()
    assert strat.calls == []


def test_reconcile_idempotent_same_target() -> None:
    strat = FakeStrategy()
    c = PreviewScrollController(select_strategy=lambda: strat)
    a = ScrollAnchor(parent_id="p", focus_chunk_seq=5)
    c.arm(a)
    c.reconcile()
    c.reconcile()
    c.reconcile()
    assert strat.calls == [a, a, a]  # always the same target — order-independent


_UNSET = object()


class _FakeWidget:
    """Duck-typed chunk/match widget: the structural scroll only reads
    ``.region`` (and an optional ``.first_match_block``)."""

    def __init__(self, region: Region, *, first_match_block: object = _UNSET) -> None:
        self.region = region
        if first_match_block is not _UNSET:
            self.first_match_block = first_match_block


class _FakePane:
    """Records the region the strategy scrolls to; no real layout."""

    def __init__(self, height: int = 40) -> None:
        self.size = Size(80, height)
        self.scroll_offset = Offset(0, 0)
        self.scrollable_content_region = Region(0, 0, 80, height)
        self.captured: Region | None = None

    def scroll_to_region(
        self,
        region: Region,
        *,
        top: bool = False,
        animate: bool = True,
        immediate: bool = False,
    ) -> None:
        self.captured = region


class _FakeHost:
    def __init__(
        self,
        pane: _FakePane,
        chunk_widgets: dict[int, object],
        match_targets: dict[int, object],
    ) -> None:
        self._pane = pane
        self._chunk_widgets = chunk_widgets
        self._match_targets = match_targets
        self.diag_msgs: list[str] = []

    def preview_pane(self) -> _FakePane:
        return self._pane

    def effective_match_spec(self) -> MatchSpec:
        return MatchSpec()

    def suppress_lazy_mount_briefly(self, duration: float = 0.4) -> None:
        return None

    def call_after_refresh(self, callback: object, *args: object, **kwargs: object) -> object:
        return None

    def diag_log(self, msg: str) -> None:
        self.diag_msgs.append(msg)

    @property
    def chunk_widgets(self) -> dict[int, object]:
        return self._chunk_widgets

    @property
    def match_targets(self) -> dict[int, object]:
        return self._match_targets


def test_structural_strategy_drops_match_a_quarter_down_the_viewport() -> None:
    # A resolved first-match block deep in the chunk is scrolled so it lands
    # ~25% down (context above it), not pinned to the viewport top.
    inner = _FakeWidget(Region(0, 100, 80, 2))
    target = _FakeWidget(Region(0, 100, 80, 40), first_match_block=inner)
    pane = _FakePane(height=40)
    host = _FakeHost(pane, chunk_widgets={5: target}, match_targets={5: target})
    strat = StructuralScrollStrategy(cast(StructuralHost, host))

    strat._do_scroll_to_chunk(5, margin_from=0.25)

    # margin = int(40 * 0.25) = 10: region.y shifts up by the margin, height grows.
    assert pane.captured == Region(0, 90, 80, 12)


def test_structural_strategy_missing_header_invokes_on_done_without_scrolling() -> None:
    pane = _FakePane()
    host = _FakeHost(pane, chunk_widgets={}, match_targets={})
    strat = StructuralScrollStrategy(cast(StructuralHost, host))
    fired: list[bool] = []

    strat._do_scroll_to_chunk(7, on_done=lambda: fired.append(True))

    assert fired == [True]
    assert pane.captured is None


class _FakeFlatBuffer:
    def __init__(self) -> None:
        self.calls: list[tuple[int, bool, float]] = []

    def scroll_to_chunk(
        self, chunk_id: int, *, prefer_first_match: bool = True, context_fraction: float = 0.0
    ) -> None:
        self.calls.append((chunk_id, prefer_first_match, context_fraction))


class _FakeFlatHost:
    def __init__(self, buf: _FakeFlatBuffer | None) -> None:
        self._buf = buf

    def active_flat_buffer(self) -> _FakeFlatBuffer | None:
        return self._buf


def test_flat_strategy_scrolls_active_buffer_to_anchor_chunk() -> None:
    buf = _FakeFlatBuffer()
    strat = FlatScrollStrategy(cast(FlatHost, _FakeFlatHost(buf)))

    strat.reconcile(ScrollAnchor(parent_id="p", focus_chunk_seq=7, context_fraction=0.25))

    # Passes the anchor's chunk + its 25% context margin through to the widget.
    assert buf.calls == [(7, True, 0.25)]


def test_flat_strategy_is_noop_without_active_buffer() -> None:
    strat = FlatScrollStrategy(cast(FlatHost, _FakeFlatHost(None)))
    # No active flat buffer → no-op, no error.
    strat.reconcile(ScrollAnchor(parent_id="p", focus_chunk_seq=7))

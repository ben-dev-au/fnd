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
        self.settled: int = 0

    def reconcile(self, anchor: ScrollAnchor, on_settled: object = None) -> None:
        self.calls.append(anchor)
        if callable(on_settled):
            on_settled()
            self.settled += 1


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


def test_reconcile_threads_on_settled_to_strategy() -> None:
    strat = FakeStrategy()
    c = PreviewScrollController(select_strategy=lambda: strat)
    c.arm(ScrollAnchor(parent_id="p", focus_chunk_seq=2))
    fired: list[bool] = []
    c.reconcile(on_settled=lambda: fired.append(True))
    assert strat.settled == 1
    assert fired == [True]


def test_reconcile_fires_on_settled_even_when_released() -> None:
    # Reveal callbacks ride on on_settled — a released (or strategy-less)
    # reconcile must still fire it, or the container is stranded hidden.
    c = PreviewScrollController(select_strategy=lambda: None)
    c.release()
    fired: list[bool] = []
    c.reconcile(on_settled=lambda: fired.append(True))
    assert fired == [True]


def test_reconcile_fires_on_settled_when_armed_but_no_strategy() -> None:
    c = PreviewScrollController(select_strategy=lambda: None)
    c.arm(ScrollAnchor(parent_id="p", focus_chunk_seq=0))
    fired: list[bool] = []
    c.reconcile(on_settled=lambda: fired.append(True))
    assert fired == [True]


_UNSET = object()


class _FakeWidget:
    """Duck-typed chunk/match widget: the structural scroll only reads
    ``.region`` (and an optional ``.first_match_block``)."""

    def __init__(self, region: Region, *, first_match_block: object = _UNSET) -> None:
        self.region = region
        self.classes: set[str] = set()
        if first_match_block is not _UNSET:
            self.first_match_block = first_match_block

    def add_class(self, name: str) -> None:
        self.classes.add(name)

    def remove_class(self, name: str) -> None:
        self.classes.discard(name)


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
        self.deferred: list[tuple[object, tuple[object, ...]]] = []

    def preview_pane(self) -> _FakePane:
        return self._pane

    def effective_match_spec(self) -> MatchSpec:
        return MatchSpec()

    def begin_reconcile_scroll(self) -> None:
        return None

    def end_reconcile_scroll(self) -> None:
        return None

    def swap_reveal_target(self, target: object, margin: int) -> bool:
        return False

    def call_after_refresh(self, callback: object, *args: object, **kwargs: object) -> object:
        self.deferred.append((callback, args))
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


def test_structural_reconcile_threads_on_settled_into_deferred_scroll() -> None:
    # With a header present, the reveal callback must ride through to
    # _do_scroll_to_chunk's on_done (the 3rd positional arg), so the
    # container is revealed only after the scroll commits.
    target = _FakeWidget(Region(0, 50, 80, 10))
    pane = _FakePane()
    host = _FakeHost(pane, chunk_widgets={3: target}, match_targets={3: target})
    strat = StructuralScrollStrategy(cast(StructuralHost, host))

    def reveal() -> None: ...

    strat.reconcile(ScrollAnchor(parent_id="p", focus_chunk_seq=3), reveal)

    assert len(host.deferred) == 1
    callback, args = host.deferred[0]
    assert callback == strat._do_scroll_to_chunk
    assert args[0] == 3  # focus seq
    assert args[2] is reveal  # on_done == on_settled


def test_structural_reconcile_fires_on_settled_when_header_absent() -> None:
    pane = _FakePane()
    host = _FakeHost(pane, chunk_widgets={}, match_targets={})
    strat = StructuralScrollStrategy(cast(StructuralHost, host))
    fired: list[bool] = []

    strat.reconcile(ScrollAnchor(parent_id="p", focus_chunk_seq=9), lambda: fired.append(True))

    assert fired == [True]
    assert host.deferred == []  # no scroll scheduled


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


def test_flat_reconcile_fires_on_settled_after_scroll() -> None:
    # The flat buffer scrolls synchronously, so the reveal fires immediately.
    buf = _FakeFlatBuffer()
    strat = FlatScrollStrategy(cast(FlatHost, _FakeFlatHost(buf)))
    fired: list[bool] = []
    strat.reconcile(ScrollAnchor(parent_id="p", focus_chunk_seq=7), lambda: fired.append(True))
    assert buf.calls == [(7, True, 0.25)]
    assert fired == [True]


def test_flat_reconcile_fires_on_settled_without_buffer() -> None:
    strat = FlatScrollStrategy(cast(FlatHost, _FakeFlatHost(None)))
    fired: list[bool] = []
    strat.reconcile(ScrollAnchor(parent_id="p", focus_chunk_seq=7), lambda: fired.append(True))
    assert fired == [True]

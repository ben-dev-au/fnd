from collections.abc import Callable
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
    ViewportLocation,
)


class FakeStrategy:
    def __init__(self) -> None:
        self.calls: list[ScrollAnchor] = []
        self.settled: int = 0
        self.restored: list[ViewportLocation] = []

    def reconcile(self, anchor: ScrollAnchor, on_settled: object = None, **_kw: object) -> None:
        self.calls.append(anchor)
        if callable(on_settled):
            on_settled()
            self.settled += 1

    def locate(self) -> ViewportLocation | None:
        return ViewportLocation("flat", line=7)

    def scroll_to_location(self, location: ViewportLocation, on_done: object = None) -> None:
        self.restored.append(location)
        if callable(on_done):
            on_done()


def test_arm_then_reconcile_calls_strategy() -> None:
    strat = FakeStrategy()
    c = PreviewScrollController(select_strategy=lambda: strat)
    a = ScrollAnchor(parent_id="p", focus_chunk_seq=3)
    c.arm(a)
    assert c.is_armed
    c.reconcile()
    assert strat.calls == [a]


def test_is_settling_true_after_arm_until_reconcile_commits() -> None:
    # A freshly-armed anchor is "settling": a nav is in flight, so lazy-mount
    # must stay suppressed. Once a reconcile commits (on_settled fires), the
    # nav has landed and lazy-mount is free to run.
    strat = FakeStrategy()
    c = PreviewScrollController(select_strategy=lambda: strat)
    c.arm(ScrollAnchor(parent_id="p", focus_chunk_seq=1))
    assert c.is_settling
    c.reconcile()  # FakeStrategy calls on_settled synchronously
    assert not c.is_settling


def test_is_settling_stays_true_until_deferred_on_settled_fires() -> None:
    # Cold/warm reveals defer on_settled (call_after_refresh). The anchor must
    # remain "settling" across that gap, then clear when the reveal lands.
    held: list[Callable[[], None]] = []

    class _DeferStrategy:
        def reconcile(
            self,
            anchor: ScrollAnchor,
            on_settled: Callable[[], None] | None = None,
            **_kw: object,
        ) -> None:
            if on_settled is not None:
                held.append(on_settled)

        def locate(self) -> ViewportLocation | None:
            return None

        def scroll_to_location(self, location: ViewportLocation, on_done: object = None) -> None:
            return None

    c = PreviewScrollController(select_strategy=lambda: _DeferStrategy())
    c.arm(ScrollAnchor(parent_id="p", focus_chunk_seq=1))
    c.reconcile(on_settled=lambda: None)
    assert c.is_settling  # scroll not committed yet
    held[0]()  # the deferred reveal/scroll lands
    assert not c.is_settling


def test_arm_resets_settled_for_next_nav() -> None:
    strat = FakeStrategy()
    c = PreviewScrollController(select_strategy=lambda: strat)
    c.arm(ScrollAnchor(parent_id="p", focus_chunk_seq=1))
    c.reconcile()
    assert not c.is_settling
    c.arm(ScrollAnchor(parent_id="p", focus_chunk_seq=9))  # a new navigation
    assert c.is_settling


def test_release_clears_is_settling() -> None:
    c = PreviewScrollController(select_strategy=lambda: FakeStrategy())
    c.arm(ScrollAnchor(parent_id="p", focus_chunk_seq=1))
    assert c.is_settling
    c.release()
    assert not c.is_settling


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
    """Duck-typed chunk/match widget: the structural scroll reads ``.region``,
    ``.virtual_region`` (content-space position for the viewport-anchor
    restore), and an optional ``.first_match_block``."""

    def __init__(
        self,
        region: Region,
        *,
        first_match_block: object = _UNSET,
        virtual_region: Region | None = None,
    ) -> None:
        self.region = region
        self.virtual_region = virtual_region if virtual_region is not None else region
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
        self.scrolled_to_y: int | None = None

    def scroll_to_region(
        self,
        region: Region,
        *,
        top: bool = False,
        animate: bool = True,
        immediate: bool = False,
    ) -> None:
        self.captured = region

    def scroll_to(self, *, y: int, animate: bool = True, immediate: bool = False) -> None:
        self.scrolled_to_y = y


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

    def swap_reveal_target(self, target: object, margin: int, anchor_region: object = None) -> bool:
        return False

    def call_after_refresh(self, callback: object, *args: object, **kwargs: object) -> object:
        self.deferred.append((callback, args))
        return None

    def above_window_pending(self, focus_chunk_seq: int) -> bool:
        # Fakes lay out synchronously, so nothing is ever still arriving above.
        return False

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
    def __init__(self, top_line: int | None = 42) -> None:
        self.calls: list[tuple[int, bool, float]] = []
        self.top_line = top_line
        self.scrolled_to: list[tuple[int, float]] = []

    def scroll_to_chunk(
        self, chunk_id: int, *, prefer_first_match: bool = True, context_fraction: float = 0.0
    ) -> None:
        self.calls.append((chunk_id, prefer_first_match, context_fraction))

    def top_logical_line(self) -> int | None:
        return self.top_line

    def scroll_to_line(
        self, line_index: int, *, center: bool = False, context_fraction: float = 0.0
    ) -> None:
        self.scrolled_to.append((line_index, context_fraction))


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


# ── Viewport anchor capture/restore (Reading View position preservation) ──


def test_controller_locate_and_scroll_to_location_delegate_to_strategy() -> None:
    strat = FakeStrategy()
    c = PreviewScrollController(select_strategy=lambda: strat)
    loc = ViewportLocation("flat", line=7)
    assert c.locate() == loc
    c.scroll_to_location(loc)
    assert strat.restored == [loc]


def test_controller_scroll_to_location_none_is_noop() -> None:
    strat = FakeStrategy()
    c = PreviewScrollController(select_strategy=lambda: strat)
    c.scroll_to_location(None)
    assert strat.restored == []


def test_controller_locate_without_strategy_returns_none() -> None:
    c = PreviewScrollController(select_strategy=lambda: None)
    assert c.locate() is None


def test_structural_locate_returns_top_chunk_and_in_chunk_offset() -> None:
    # The fake pane's viewport top is at scrollable_content_region.y (0). A
    # chunk starting 3 rows above that spans the top → located with offset 3.
    top_chunk = _FakeWidget(Region(0, -3, 80, 10))
    pane = _FakePane(height=40)
    host = _FakeHost(pane, chunk_widgets={2: top_chunk}, match_targets={})
    strat = StructuralScrollStrategy(cast(StructuralHost, host))

    assert strat.locate() == ViewportLocation("structural", chunk_seq=2, offset=3)


def test_structural_scroll_to_location_scrolls_to_chunk_plus_offset() -> None:
    # virtual_region.y (content-space top) = 200; restore scrolls to top + the
    # captured 6-row in-chunk offset.
    w = _FakeWidget(Region(0, 100, 80, 40), virtual_region=Region(0, 200, 80, 40))
    pane = _FakePane(height=40)
    host = _FakeHost(pane, chunk_widgets={5: w}, match_targets={})
    strat = StructuralScrollStrategy(cast(StructuralHost, host))

    strat.scroll_to_location(ViewportLocation("structural", chunk_seq=5, offset=6))

    assert pane.scrolled_to_y == 206


def _drain(host: _FakeHost, *, limit: int = 200) -> int:
    """Run the host's deferred callbacks (one per simulated refresh) until the
    chain stops rescheduling. Returns how many refreshes it took."""
    refreshes = 0
    while host.deferred and refreshes < limit:
        cb, args = host.deferred.pop(0)
        assert callable(cb)
        cb(*args)
        refreshes += 1
    return refreshes


def test_controller_is_restoring_until_the_strategy_reports_done() -> None:
    # The restore re-scrolls across many refreshes and never arms the anchor, so
    # is_settling can't cover it. is_restoring is what a caller waits on.
    held: list[object] = []

    class _SlowStrategy(FakeStrategy):
        def scroll_to_location(self, location: ViewportLocation, on_done: object = None) -> None:
            self.restored.append(location)
            held.append(on_done)  # not called — the reflow is still running

    strat = _SlowStrategy()
    c = PreviewScrollController(select_strategy=lambda: strat)
    assert not c.is_restoring
    c.scroll_to_location(ViewportLocation("flat", line=7))
    assert c.is_restoring
    done = held[0]
    assert callable(done)
    done()
    assert not c.is_restoring


def test_controller_is_restoring_clears_when_the_strategy_raises() -> None:
    # A raising strategy must not strand the flag — nothing would ever clear it.
    c = PreviewScrollController(select_strategy=lambda: _RaisingStrategy())
    c.scroll_to_location(ViewportLocation("flat", line=3))
    assert not c.is_restoring


def test_structural_restore_extends_its_budget_while_the_layout_still_moves() -> None:
    # The re-wrap keeps moving the chunk's content position. A fixed refresh
    # budget loses the restore on a slow runner, so a still-moving layout earns
    # another budget (capped) rather than the chain giving up mid-reflow.
    w = _FakeWidget(Region(0, 100, 80, 40), virtual_region=Region(0, 200, 80, 40))
    pane = _FakePane(height=40)
    host = _FakeHost(pane, chunk_widgets={5: w}, match_targets={})
    strat = StructuralScrollStrategy(cast(StructuralHost, host))
    finished: list[bool] = []

    moved = 0

    def _keep_moving() -> None:
        # Simulate the re-wrap: the content position shifts every refresh for
        # longer than the base budget.
        nonlocal moved
        moved += 1
        w.virtual_region = Region(0, 200 + moved * 5, 80, 40)

    original = host.call_after_refresh

    def _tracking(callback: object, *args: object, **kwargs: object) -> object:
        _keep_moving()
        return original(callback, *args, **kwargs)

    host.call_after_refresh = _tracking  # type: ignore[method-assign]
    strat.scroll_to_location(
        ViewportLocation("structural", chunk_seq=5, offset=6),
        lambda: finished.append(True),
    )
    refreshes = _drain(host)

    assert refreshes > 12, "a still-moving reflow must outlast the base budget"
    assert finished == [True], "the restore must still report done at the ceiling"


def test_structural_restore_reports_done_once_the_layout_settles() -> None:
    w = _FakeWidget(Region(0, 100, 80, 40), virtual_region=Region(0, 200, 80, 40))
    pane = _FakePane(height=40)
    host = _FakeHost(pane, chunk_widgets={5: w}, match_targets={})
    strat = StructuralScrollStrategy(cast(StructuralHost, host))
    finished: list[bool] = []

    strat.scroll_to_location(
        ViewportLocation("structural", chunk_seq=5, offset=6),
        lambda: finished.append(True),
    )
    assert finished == [], "done must not fire before the re-applies have run"
    _drain(host)

    assert finished == [True]
    assert pane.scrolled_to_y == 206


def test_structural_scroll_to_location_ignores_flat_location() -> None:
    pane = _FakePane()
    host = _FakeHost(pane, chunk_widgets={}, match_targets={})
    strat = StructuralScrollStrategy(cast(StructuralHost, host))

    strat.scroll_to_location(ViewportLocation("flat", line=3))  # wrong kind

    assert pane.scrolled_to_y is None


def test_flat_locate_returns_top_logical_line() -> None:
    buf = _FakeFlatBuffer(top_line=42)
    strat = FlatScrollStrategy(cast(FlatHost, _FakeFlatHost(buf)))
    assert strat.locate() == ViewportLocation("flat", line=42)


def test_flat_locate_none_without_buffer() -> None:
    strat = FlatScrollStrategy(cast(FlatHost, _FakeFlatHost(None)))
    assert strat.locate() is None


def test_flat_scroll_to_location_scrolls_to_logical_line_without_margin() -> None:
    buf = _FakeFlatBuffer()
    strat = FlatScrollStrategy(cast(FlatHost, _FakeFlatHost(buf)))

    strat.scroll_to_location(ViewportLocation("flat", line=42))

    # Exact restore: the logical line, no context margin.
    assert buf.scrolled_to == [(42, 0.0)]


# ── on_settled / error-resilience contract (PR #22 review) ──


class _RaisingStrategy:
    """Strategy whose reconcile raises WITHOUT first calling on_settled."""

    def reconcile(self, anchor: ScrollAnchor, on_settled: object = None, **_kw: object) -> None:
        raise RuntimeError("strategy boom")

    def locate(self) -> ViewportLocation | None:
        raise RuntimeError("locate boom")

    def scroll_to_location(self, location: ViewportLocation, on_done: object = None) -> None:
        raise RuntimeError("scroll boom")


def test_reconcile_fires_on_settled_when_strategy_raises_then_reraises() -> None:
    # The reveal/swap rides on_settled — it must fire even if the strategy
    # raises before settling, and the (unexpected) error is still surfaced.
    c = PreviewScrollController(select_strategy=lambda: _RaisingStrategy())
    c.arm(ScrollAnchor(parent_id="p", focus_chunk_seq=1))
    fired: list[bool] = []
    raised = False
    try:
        c.reconcile(on_settled=lambda: fired.append(True))
    except RuntimeError:
        raised = True
    assert raised
    assert fired == [True]


def test_locate_returns_none_when_strategy_raises() -> None:
    # Best-effort: a failing locate must not break the caller (reading toggle).
    c = PreviewScrollController(select_strategy=lambda: _RaisingStrategy())
    assert c.locate() is None


def test_scroll_to_location_swallows_strategy_error() -> None:
    c = PreviewScrollController(select_strategy=lambda: _RaisingStrategy())
    c.scroll_to_location(ViewportLocation("flat", line=3))  # must not raise


def test_structural_do_scroll_fires_on_done_even_if_scroll_raises() -> None:
    # An exception in the scroll body must not drop on_done — otherwise the
    # pre-reveal container would stay hidden (blank, stuck) on a cold mount.
    class _BoomPane(_FakePane):
        def scroll_to_region(
            self,
            region: Region,
            *,
            top: bool = False,
            animate: bool = True,
            immediate: bool = False,
        ) -> None:
            raise RuntimeError("scroll boom")

    target = _FakeWidget(Region(0, 50, 80, 10))
    pane = _BoomPane(height=40)
    host = _FakeHost(pane, chunk_widgets={3: target}, match_targets={3: target})
    strat = StructuralScrollStrategy(cast(StructuralHost, host))
    fired: list[bool] = []
    strat._do_scroll_to_chunk(3, retries=0, on_done=lambda: fired.append(True))
    assert fired == [True]


class _CallsThenRaisesStrategy:
    """Strategy that calls on_settled itself, THEN raises — the flat-path
    shape where the synchronous on_settled() is the last thing before any
    error can escape. The controller's error path must not call it a second
    time (fire-once contract)."""

    def reconcile(self, anchor: ScrollAnchor, on_settled: object = None, **_kw: object) -> None:
        if callable(on_settled):
            on_settled()
        raise RuntimeError("boom after settle")

    def locate(self) -> ViewportLocation | None:
        return None

    def scroll_to_location(self, location: ViewportLocation, on_done: object = None) -> None:
        return None


def test_reconcile_does_not_double_fire_on_settled_when_strategy_calls_then_raises() -> None:
    c = PreviewScrollController(select_strategy=lambda: _CallsThenRaisesStrategy())
    c.arm(ScrollAnchor(parent_id="p", focus_chunk_seq=1))
    calls: list[bool] = []
    raised = False
    try:
        c.reconcile(on_settled=lambda: calls.append(True))
    except RuntimeError:
        raised = True
    assert raised  # error still surfaced
    assert calls == [True]  # fired EXACTLY once, not twice


def test_scroll_waits_while_content_above_the_match_is_still_arriving() -> None:
    """The match's screen position is decided by how much content sits above it,
    and that content arrives late: chunk widgets mount at ~0 height and grow as
    their markdown lays out. Committing before it settles lands correctly and
    then slides — measured at 17 rows on a real book page, leaving the match two
    thirds down the pane instead of a quarter.

    ``build_done`` is NOT the signal to wait on: on that page the chunks above
    were all mounted with build_done set and still grew 142 -> 159 rows. Height
    has to stop changing.
    """
    pane = _FakePane()
    above = _FakeWidget(Region(0, 0, 80, 10))
    target = _FakeWidget(Region(0, 40, 80, 3))
    host = _FakeHost(pane, chunk_widgets={1: above, 5: target}, match_targets={5: target})
    strat = StructuralScrollStrategy(cast(StructuralHost, host))

    strat._do_scroll_to_chunk(5, on_done=None)
    assert pane.captured is None, "committed while the content above was unmeasured"

    # Same height twice running = settled, so the commit goes through.
    strat._do_scroll_to_chunk(5, on_done=None, above_height=10, stable_ticks=1)
    assert pane.captured is not None


def test_a_growing_region_above_defers_the_commit() -> None:
    pane = _FakePane()
    above = _FakeWidget(Region(0, 0, 80, 27))
    target = _FakeWidget(Region(0, 40, 80, 3))
    host = _FakeHost(pane, chunk_widgets={1: above, 5: target}, match_targets={5: target})
    strat = StructuralScrollStrategy(cast(StructuralHost, host))

    # Last seen 10 rows, now 27: still growing, so the stability count resets.
    strat._do_scroll_to_chunk(5, on_done=None, above_height=10, stable_ticks=1)

    assert pane.captured is None


def test_the_wait_is_bounded_so_a_restless_layout_still_lands() -> None:
    """A page whose layout never quite settles must not hold the scroll open —
    landing late is worse than landing a few rows off."""
    pane = _FakePane()
    above = _FakeWidget(Region(0, 0, 80, 27))
    target = _FakeWidget(Region(0, 40, 80, 3))
    host = _FakeHost(pane, chunk_widgets={1: above, 5: target}, match_targets={5: target})
    strat = StructuralScrollStrategy(cast(StructuralHost, host))

    strat._do_scroll_to_chunk(5, retries=21, on_done=None, above_height=10, stable_ticks=0)

    assert pane.captured is not None


def test_an_unlaid_out_chunk_above_is_not_mistaken_for_settled() -> None:
    """Zero height is the absence of a measurement, not a stable one.

    A preceding chunk mounts at height 0 and grows on a later refresh. Reading
    three zeroes as two stable ticks commits the scroll, and the chunk then lays
    out and pushes the match down — the same class of drift the settle gate
    exists to prevent, entered from the other side.
    """
    pane = _FakePane()
    above = _FakeWidget(Region(0, 0, 80, 0))  # mounted, not yet laid out
    target = _FakeWidget(Region(0, 40, 80, 3))
    host = _FakeHost(pane, chunk_widgets={1: above, 5: target}, match_targets={5: target})
    strat = StructuralScrollStrategy(cast(StructuralHost, host))

    for ticks in (0, 1, 2):
        strat._do_scroll_to_chunk(5, on_done=None, above_height=0, stable_ticks=ticks)
        assert pane.captured is None, f"committed against a zero-height chunk (ticks={ticks})"


def test_a_genuinely_empty_chunk_above_still_lands() -> None:
    """The wait is bounded, so a chunk that is legitimately zero-height cannot
    stall navigation for ever."""
    pane = _FakePane()
    above = _FakeWidget(Region(0, 0, 80, 0))
    target = _FakeWidget(Region(0, 40, 80, 3))
    host = _FakeHost(pane, chunk_widgets={1: above, 5: target}, match_targets={5: target})
    strat = StructuralScrollStrategy(cast(StructuralHost, host))

    strat._do_scroll_to_chunk(5, retries=21, on_done=None, above_height=0, stable_ticks=0)

    assert pane.captured is not None

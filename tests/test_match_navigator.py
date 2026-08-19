"""MatchNavigator: viewport-hop stepping, wrap, and burst/manual-scroll
memory, driven through injected stops + a fake pane so the logic tests
without a running app."""

from __future__ import annotations

from textual.geometry import Offset, Region, Size

from fnd.tui.match_navigator import MatchNavigator


class FakePane:
    def __init__(self, vh: int) -> None:
        self.size = Size(80, vh)
        self.scroll_offset = Offset(0, 0)
        self.scrollable_content_region = Region(0, 0, 80, vh)
        self.scrolled: list[int] = []

    def scroll_to_region(self, region: Region, **_kw: object) -> None:
        self.scrolled.append(region.y)
        self.scroll_offset = Offset(0, region.y)


class FakeApp:
    """Minimal stand-in: no `_preview` (skips reconcile), no-op UI refreshers.
    ``call_after_refresh`` runs inline so a scheduled re-measure resolves
    synchronously within the test."""

    def _diag_log(self, msg: str) -> None:
        # Modelled, not stubbed away: _scroll_to_stop logs the scroll it commits,
        # and a stand-in without it would hide that from these tests.
        pass

    def _refresh_preview_match_indicator(self) -> None:
        pass

    def _refresh_footer_hints(self) -> None:
        pass

    def call_after_refresh(self, callback: object, *args: object, **kwargs: object) -> None:
        callback(*args, **kwargs)  # type: ignore[operator]


def _nav(stops: list[int], vh: int = 20) -> MatchNavigator:
    nav = MatchNavigator.__new__(MatchNavigator)  # bypass app wiring
    nav._app = FakeApp()  # type: ignore[assignment]
    nav._last_target = None
    nav._count = len(stops)
    nav._above = 0
    nav._below = 0
    nav._measure_pending = False
    # Measurement polls are tied to the rebuild generation so a superseded one
    # can't repopulate a newer preview's counts; the stand-in has to carry it.
    nav._refresh_gen = 0
    pane = FakePane(vh)
    # Inject the pane + a fixed region-stop list so _go/next use them, plus a
    # wide chunk extent so scoping keeps every stop (the app derives this from
    # the current result's widget; here the injected stops ARE the chunk).
    nav._pane = lambda: pane  # type: ignore[assignment]
    nav._region_stops = lambda _p: stops  # type: ignore[assignment]
    nav._current_chunk_extent = lambda _p: (0, 10**9)  # type: ignore[assignment]
    return nav


def test_next_advances_by_viewport_and_wraps() -> None:
    nav = _nav([5, 8, 40, 45, 90])
    nav.next()
    assert nav._last_target == 2  # first stop below the viewport
    nav.next()
    assert nav._last_target == 4  # viewport hop, not 45
    nav.next()
    assert nav._last_target == 0  # wrap to first
    assert nav.count == 5
    assert nav.position == 1


def test_burst_uses_viewport_derived_margin() -> None:
    # On a 24-row pane the match lands int(24*0.25)=6 rows down, not the old
    # fixed 4. After landing on stop 30 (viewport top 24, bottom 48), the next
    # hop must reveal stop 49 — with the stale margin=4 the reference bottom
    # would be 50 and it would skip 49 straight to 60.
    nav = _nav([0, 30, 49, 60], vh=24)
    nav.next()
    assert nav._last_target == 1
    nav.next()
    assert nav._last_target == 2  # stop 49, not skipped to 60 (index 3)


def test_prev_and_manual_scroll_reset() -> None:
    nav = _nav([5, 8, 40, 45, 90])
    nav._pane().scroll_offset = Offset(0, 80)  # type: ignore[attr-defined]
    nav.prev()
    assert nav._last_target == 2
    nav.on_manual_scroll()
    assert nav._last_target is None


def test_next_updates_offscreen_views() -> None:
    # vh=20; jump to stop 40 lands the viewport at [35, 55) (40 dropped a
    # quarter down). Stops 5,8 (one screenful) are then above; 90 is one below.
    nav = _nav([5, 8, 40, 45, 90])
    assert nav.above == 0
    assert nav.below == 0
    nav.next()
    assert nav._last_target == 2
    assert nav.above == 1  # 5 and 8 fall in a single screenful → one view
    assert nav.below == 1  # 90


def test_on_result_revealed_clears_stale_then_remeasures() -> None:
    # Simulate a switch INTO a result: stale markers from the previous result
    # must be dropped and re-derived for the new viewport. Stops [5, 100], vh=20,
    # scroll 0 → viewport [0, 20): 100 is one view below, nothing above.
    nav = _nav([5, 100], vh=20)
    nav._above, nav._below = 9, 9  # garbage left over from a prior result
    nav._last_target = 3
    nav.on_result_revealed()  # FakeApp runs the coalesced re-measure inline
    assert nav._last_target is None  # burst memory reset for the new result
    assert nav.above == 0
    assert nav.below == 1  # re-measured for the current viewport, not the stale 9


def test_no_stops_is_noop() -> None:
    nav = _nav([])
    nav.next()
    assert nav._last_target is None
    assert nav.count == 0
    assert nav.position is None


def _hint_nav(current: object, *, match_target: object = None) -> MatchNavigator:
    """A navigator whose focused chunk is ``current``, wired only as far as
    ``current_chunk_has_stops`` reaches."""
    from fnd.matching import MatchSpec

    class _Anchor:
        focus_chunk_seq = 7

    class _Ctrl:
        anchor = _Anchor()

    class _Preview:
        def __init__(self) -> None:
            self.chunk_widgets: dict[int, object] = {7: current}
            self.match_targets: dict[int, object] = (
                {7: match_target} if match_target is not None else {}
            )

    app = FakeApp()
    app._effective_match_spec = MatchSpec.from_query("quartzfin")  # type: ignore[attr-defined]
    app._preview_scroll = _Ctrl()  # type: ignore[attr-defined]
    app._preview = _Preview()  # type: ignore[attr-defined]

    nav = MatchNavigator.__new__(MatchNavigator)
    nav._app = app  # type: ignore[assignment]
    nav._count = 0  # the file-wide fallback must NOT be what carries this
    return nav


def test_a_frozen_focused_chunk_still_reports_its_stops() -> None:
    """The footer hint must follow what ``n``/``b`` can actually reach.

    Serving a capture replaces the chunk's widget tree with a ``FrozenChunkView``
    and pops its match target. ``enumerate_stop_regions`` handles that view, so
    ``n``/``b`` keep working — but without a matching branch here the plain-chunk
    fallback reads the popped target, returns False, and the app hides the
    ``n/b Matches`` hint on a chunk where both keys work.
    """
    from fnd.tui.preview.frozen import FrozenChunk, FrozenChunkView

    with_stops = FrozenChunkView(FrozenChunk(chunk_seq=7, width=80, strips=[], stop_rows=[3, 9]))
    assert _hint_nav(with_stops).current_chunk_has_stops()

    without = FrozenChunkView(FrozenChunk(chunk_seq=7, width=80, strips=[], stop_rows=[]))
    assert not _hint_nav(without).current_chunk_has_stops(), (
        "a capture with no recorded stops must not advertise the hint"
    )

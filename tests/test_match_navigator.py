"""MatchNavigator: viewport-hop stepping, wrap, and burst/manual-scroll
memory, driven through injected stops + a fake pane so the logic tests
without a running app."""

from __future__ import annotations

from types import SimpleNamespace

from textual.geometry import Offset, Region, Size

from fnd.tui.match_navigator import MatchNavigator


class FakePane:
    """``max_scroll_y`` is derived and the scroll clamps to it: a pane that
    always reaches the position it is asked for hides the stranded-view case."""

    def __init__(self, vh: int, virtual_height: int = 10**6) -> None:
        self.size = Size(80, vh)
        self.scroll_offset = Offset(0, 0)
        self.scrollable_content_region = Region(0, 0, 80, vh)
        self.virtual_height = virtual_height
        self.virtual_size = Size(80, virtual_height)
        self.scrolled: list[int] = []

    @property
    def max_scroll_y(self) -> int:
        return max(0, self.virtual_height - self.size.height)

    def scroll_to_region(self, region: Region, **_kw: object) -> None:
        y = min(region.y, self.max_scroll_y)
        self.scrolled.append(y)
        self.scroll_offset = Offset(0, y)


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

    # The burst memory is keyed on the revealed chunk, so the stand-in has one.
    _preview_scroll = SimpleNamespace(anchor=SimpleNamespace(focus_chunk_seq=1), is_settling=False)


def _nav(stops: list[int], vh: int = 20, virtual_height: int = 10**6) -> MatchNavigator:
    # The real ``__init__`` against a fake app, not a hand-copied set of its
    # fields: it only stores ``app`` and zeroes plain attributes, and copying it
    # here made every new piece of navigator state an AttributeError in these
    # tests instead of in the code that forgot it.
    nav = MatchNavigator(FakeApp())  # type: ignore[arg-type]
    nav._count = len(stops)
    pane = FakePane(vh, virtual_height)
    # Inject the pane + a fixed region-stop list so _go/next use them, plus a
    # wide chunk extent so scoping keeps every stop (the app derives this from
    # the current result's widget; here the injected stops ARE the chunk).
    nav._pane = lambda: pane  # type: ignore[assignment]
    nav._region_stops = lambda _p: stops  # type: ignore[assignment]
    # Extent from 0, so the offsets the navigator stores (measured from the
    # chunk's top) read as absolute content y in these tests.
    nav._current_chunk_extent = lambda _p: (0, 10**9)  # type: ignore[assignment]
    return nav


def test_next_advances_by_viewport_and_wraps() -> None:
    # The pane is at 0, which shows stop 5, so vh=20 tiles [5,8,40,45,90] into
    # views at 0 (the landing), 40 and 90.
    nav = _nav([5, 8, 40, 45, 90])
    nav.next()
    assert nav._last_rel == 40  # the first view not already on screen
    nav.next()
    assert nav._last_rel == 90  # a whole viewport on, not 45
    nav.next()
    assert nav._last_rel == 0  # wrap to the landing
    assert nav.count == 5


def test_a_stop_inside_a_view_is_shown_not_skipped() -> None:
    # vh=24 tiles [0,30,49,60] into views at 0, 30 and 60. 49 gets no view of
    # its own because the view at 30 spans [30,54) and already shows it — the
    # hop must not stop twice on one screenful, nor skip 49 out of sight.
    nav = _nav([0, 30, 49, 60], vh=24)
    nav.next()
    assert nav._last_rel == 30
    assert 49 < 30 + 24, "49 must be visible from the view at 30 for this to hold"
    nav.next()
    assert nav._last_rel == 60


def test_prev_and_manual_scroll_reset() -> None:
    nav = _nav([5, 8, 40, 45, 90])
    nav._pane().scroll_offset = Offset(0, 80)  # type: ignore[attr-defined]
    nav.prev()
    assert nav._last_rel == 40
    nav.on_manual_scroll()
    assert nav._last_rel is None


def test_next_updates_offscreen_views() -> None:
    # vh=20; the hop to the view at 40 puts the viewport at [40, 60). Stops 5
    # and 8 share the view above; 90 is the one below.
    nav = _nav([5, 8, 40, 45, 90])
    assert nav.above == 0
    assert nav.below == 0
    nav.next()
    assert nav._last_rel == 40
    assert nav.above == 1  # 5 and 8 fall in a single screenful → one view
    assert nav.below == 1  # 90


def test_on_result_revealed_clears_stale_then_remeasures() -> None:
    # Simulate a switch INTO a result: stale markers from the previous result
    # must be dropped and re-derived for the new viewport. Stops [5, 100], vh=20,
    # scroll 0 → viewport [0, 20): 100 is one view below, nothing above.
    nav = _nav([5, 100], vh=20)
    nav._above, nav._below = 9, 9  # garbage left over from a prior result
    nav._last_rel, nav._last_seq = 30, 1
    nav.on_result_revealed()  # FakeApp runs the coalesced re-measure inline
    assert nav._last_rel is None  # burst memory reset for the new result
    assert nav.above == 0
    assert nav.below == 1  # re-measured for the current viewport, not the stale 9


def test_no_stops_is_noop() -> None:
    nav = _nav([])
    nav.next()
    assert nav._last_rel is None
    assert nav.count == 0


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

    nav = MatchNavigator(app)  # type: ignore[arg-type]
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


def test_burst_memory_from_another_result_is_not_resumed() -> None:
    """A reveal that does not fire leaves the previous result's position behind.
    Resuming from a position this chunk has no view at sent the first press to
    the wrap instead of the next view, and made n move on a chunk that fits."""
    nav = _nav([5, 8, 40, 45, 90])
    nav._last_rel, nav._last_seq = 270, 9  # a view of the result just left

    nav.next()

    assert nav._last_rel == 40, "n resumed from a foreign position"


def test_a_position_from_another_chunk_is_not_resumed_even_if_it_lines_up() -> None:
    """An absolute y is ambiguous across chunks: a neighbour's view can land on
    exactly one of this chunk's anchors, and membership alone would accept it."""
    nav = _nav([5, 8, 40, 45, 90])
    nav._last_rel, nav._last_seq = 40, 9  # a real anchor here, but another chunk

    nav.next()

    assert nav._last_rel == 40, "resumed from a neighbour's position that lined up"
    assert nav._pane().scrolled == [40]  # type: ignore[attr-defined]


def test_a_chunk_that_fits_does_not_move_under_n() -> None:
    nav = _nav([5, 8, 15])
    nav._pane().scroll_offset = Offset(0, 0)  # type: ignore[attr-defined]

    nav.next()
    nav.prev()

    assert nav._pane().scrolled == [], "n/b scrolled a chunk with nothing off screen"  # type: ignore[attr-defined]


def test_a_view_past_the_last_screenful_merges_into_it() -> None:
    """The document's last screenful is ONE view however many matches fall in
    it. An anchor the pane can never scroll to left ▼ stuck above zero with n
    scrolling nowhere."""
    # vh=20, document 120 rows → max_scroll_y=100. The view at 105 is past the
    # furthest the pane can scroll, so the clamped viewport [100,120) is what
    # shows it — and 118 shares that view.
    nav = _nav([0, 105, 118], vh=20, virtual_height=120)

    nav.next()
    assert nav._last_rel == 105
    assert nav.below == 0, "a view beyond the last screenful was still counted"

    nav.next()  # wraps; must not stall on an unreachable anchor
    assert nav._last_rel == 0


def test_a_key_with_nowhere_to_go_still_refreshes_the_border() -> None:
    """The no-op path has no scroll to trigger a re-measure, so a border left
    over from the previous result would stand until the next navigation."""
    nav = _nav([5, 8, 15])  # one view, all of it on screen
    nav._above, nav._below = 4, 7  # stale counts from the result just left

    nav.next()

    assert (nav.above, nav.below) == (0, 0)


class _StubWidget:
    """A mounted chunk widget with a given laid-out height (0 = not arranged)."""

    def __init__(self, y: int, height: int) -> None:
        self.region = Region(0, y, 80, height)


def _extent_nav(widgets: dict[int, _StubWidget], seq: int, virtual_height: int = 500):
    """A navigator whose _current_chunk_extent runs for real over ``widgets``."""
    from types import SimpleNamespace

    nav = MatchNavigator(FakeApp())  # type: ignore[arg-type]
    pane = FakePane(40, virtual_height)
    nav._pane = lambda: pane  # type: ignore[assignment]
    nav._app = SimpleNamespace(  # type: ignore[assignment]
        _preview_scroll=SimpleNamespace(anchor=SimpleNamespace(focus_chunk_seq=seq)),
        _preview=SimpleNamespace(chunk_widgets=widgets),
    )
    return nav, pane


def test_an_unlaid_chunk_scopes_to_nothing_not_to_everything() -> None:
    """The background fill hides every above-window chunk. Returning None there
    means "no per-chunk widgets", which sends _chunk_stops to its whole-preview
    fallback — and that is how n/b walked out of the current result."""
    nav, pane = _extent_nav({5: _StubWidget(0, 0), 6: _StubWidget(120, 40)}, seq=5)

    assert nav._current_chunk_extent(pane) == (0, 0)  # type: ignore[arg-type]

    nav._region_stops = lambda _p: [10, 200, 300]  # type: ignore[assignment]
    assert nav._chunk_stops(pane) == [], "an unlaid chunk handed back the whole preview"  # type: ignore[arg-type]


def test_a_plain_chunk_extends_past_its_first_line() -> None:
    """chunk_widgets holds only the FIRST LINE of a pdf/txt chunk, so bounding on
    the widget's own height collapses the chunk to one row and n/b go inert."""
    # seq 5 is a plain chunk's first line (1 row); seq 6 has not arranged yet.
    nav, pane = _extent_nav(
        {5: _StubWidget(0, 1), 6: _StubWidget(0, 0), 7: _StubWidget(120, 40)}, seq=5
    )

    assert nav._current_chunk_extent(pane) == (0, 120)  # type: ignore[arg-type]


def test_the_extent_stops_at_the_next_laid_out_chunk() -> None:
    nav, pane = _extent_nav({5: _StubWidget(0, 60), 6: _StubWidget(60, 40)}, seq=5)

    assert nav._current_chunk_extent(pane) == (0, 60)  # type: ignore[arg-type]


def test_a_chunk_with_no_stops_still_refreshes_the_border() -> None:
    """The empty-stops return does not scroll either, so nothing else clears the
    counts the previous result left behind."""
    nav = _nav([])
    nav._above, nav._below = 4, 7

    nav.next()

    assert (nav.above, nav.below) == (0, 0)


def test_the_landing_is_the_first_view_so_b_returns_to_it() -> None:
    """A results landing sits a quarter-viewport above the match. Tiling from the
    match instead makes the landing a place no key can return to: the first press
    is spent snapping onto the tiling, and b then reports a different count than
    the landing showed."""
    nav = _nav([20, 33, 46, 59, 71, 83, 96], vh=40)
    nav._pane().scroll_offset = Offset(0, 10)  # type: ignore[attr-defined]

    nav.next()
    landed = nav._last_rel
    nav.prev()

    assert nav._home_rel == 10, "the landing was not pinned as the first view"
    assert landed == 59, "n did not move a whole viewport on"
    assert nav._last_rel == 10, "b did not return to the landing"


def test_a_landing_from_another_chunk_is_not_reused() -> None:
    nav = _nav([20, 33, 46, 59, 71, 83, 96], vh=40)
    nav._home_rel, nav._home_seq = 900, 9  # a landing in the result just left
    nav._pane().scroll_offset = Offset(0, 10)  # type: ignore[attr-defined]

    nav.next()

    assert nav._home_rel == 10, "a foreign landing anchored this chunk's views"


def test_a_reflow_under_the_walk_does_not_strand_the_landing() -> None:
    """A mount above the window shifts every stop (measured: +5 rows between two
    presses). The landing and the last hop are stored as offsets from the chunk's
    top, so they move with it instead of pointing at where the content used to be.
    """
    stops = [15, 28, 41, 54, 66, 78, 91]
    nav = _nav(stops, vh=35)
    nav._pane().scroll_offset = Offset(0, 7)  # type: ignore[attr-defined]
    nav.next()
    assert nav._last_rel == 54, "n did not hop a whole viewport on"

    # Everything above the chunk grew by 5: the chunk and its stops move down.
    shifted = [y + 5 for y in stops]
    nav._region_stops = lambda _p: shifted  # type: ignore[assignment]
    nav._current_chunk_extent = lambda _p: (5, 10**9)  # type: ignore[assignment]
    nav._pane().scroll_offset = Offset(0, 59)  # type: ignore[attr-defined]

    nav.prev()

    assert nav._last_rel == 7, "b did not return to the landing after the reflow"
    assert nav._pane().scrolled[-1] == 12  # type: ignore[attr-defined]

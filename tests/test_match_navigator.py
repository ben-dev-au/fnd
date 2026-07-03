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
    """Minimal stand-in: no `_preview` (skips reconcile), no-op UI refreshers."""

    def _refresh_preview_match_indicator(self) -> None:
        pass

    def _refresh_footer_hints(self) -> None:
        pass


def _nav(stops: list[int], vh: int = 20) -> MatchNavigator:
    nav = MatchNavigator.__new__(MatchNavigator)  # bypass app wiring
    nav._app = FakeApp()  # type: ignore[assignment]
    nav._last_target = None
    nav._margin = 4
    nav._count = len(stops)
    pane = FakePane(vh)
    # Inject the pane + a fixed region-stop list so _go/next use them.
    nav._pane = lambda: pane  # type: ignore[assignment]
    nav._region_stops = lambda _p: stops  # type: ignore[assignment]
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


def test_prev_and_manual_scroll_reset() -> None:
    nav = _nav([5, 8, 40, 45, 90])
    nav._pane().scroll_offset = Offset(0, 80)  # type: ignore[attr-defined]
    nav.prev()
    assert nav._last_target == 2
    nav.on_manual_scroll()
    assert nav._last_target is None


def test_no_stops_is_noop() -> None:
    nav = _nav([])
    nav.next()
    assert nav._last_target is None
    assert nav.count == 0
    assert nav.position is None

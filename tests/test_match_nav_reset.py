"""Match-nav burst memory clears on manual scroll."""

from __future__ import annotations

from fnd.tui.match_navigator import MatchNavigator


class _StubApp:
    """Absorbs the coalesced re-measure ``on_manual_scroll`` now schedules;
    the callback is dropped so this test stays focused on burst memory."""

    def call_after_refresh(self, callback: object, *a: object, **k: object) -> None:
        pass


def test_manual_scroll_clears_the_burst_memory() -> None:
    nav = MatchNavigator(_StubApp())  # type: ignore[arg-type]
    nav._last_rel = 30
    nav.on_manual_scroll()
    assert nav._last_rel is None

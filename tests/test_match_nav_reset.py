"""Match-nav burst memory clears on manual scroll."""

from __future__ import annotations

from fnd.tui.match_navigator import MatchNavigator


def test_manual_scroll_clears_last_target() -> None:
    nav = MatchNavigator.__new__(MatchNavigator)
    nav._last_target = 3
    nav.on_manual_scroll()
    assert nav._last_target is None

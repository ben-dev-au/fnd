"""Match-nav burst memory clears on manual scroll."""

from __future__ import annotations

from fnd.tui.match_navigator import MatchNavigator


class _StubApp:
    """Absorbs the coalesced re-measure ``on_manual_scroll`` now schedules;
    the callback is dropped so this test stays focused on burst memory."""

    def call_after_refresh(self, callback: object, *a: object, **k: object) -> None:
        pass


def test_manual_scroll_clears_last_target() -> None:
    # The real constructor against a stub app: hand-copying its fields made
    # every new piece of navigator state fail here rather than where it was
    # forgotten.
    nav = MatchNavigator(_StubApp())  # type: ignore[arg-type]
    nav._last_target = 3
    nav.on_manual_scroll()
    assert nav._last_target is None

"""One navigation must commit at most one scroll.

A navigation reconciles more than once: the finalise commits the landing, then
the background fill re-anchors after revealing the chunks it mounted above.
``_generation`` only cancels chains from an OLDER navigation, so both ran at the
same generation and neither cancelled the other — two chains, two committed
scrolls, and the retry budget spent twice (measured ~62 ticks and >1 commit on
22 of 30 navigations).

Two scrolls landing at different times is what the user sees as the preview
settling in steps rather than going straight to the match, so "at most one
commit" is the invariant worth pinning rather than any particular timing.

Pinned at the CONTROLLER, not through a driven app. An end-to-end version of
this was written first and thrown away: it passed with the fix reverted, because
the autouse fixtures pin preview debounce and prefetch to 0 and the second chain
never materialises in-suite. A test that cannot fail is worse than no test. The
end-to-end behaviour is measured instead by ``dev/tools/nav_jump_probe.py``,
which counts committed scrolls per navigation against a real corpus at real
defaults (22/30 navigations with more than one commit before this, 3/30 after).
"""

from __future__ import annotations

from collections.abc import Callable

from fnd.tui.preview_scroll import (
    PreviewScrollController,
    ScrollAnchor,
    ViewportLocation,
)


class _Strategy:
    """Records reconcile calls and lets the test fire their callbacks by hand,
    so chain ordering is explicit rather than timing-dependent."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, Callable[[], None] | None]] = []

    def reconcile(
        self,
        anchor: ScrollAnchor,
        on_settled: Callable[[], None] | None = None,
        *,
        generation: int = 0,
        current_generation: Callable[[], int] | None = None,
    ) -> None:
        self.calls.append((generation, on_settled))

    def locate(self) -> ViewportLocation | None:
        return None

    def hold_location(self, location: ViewportLocation) -> None:
        return None

    def scroll_to_location(
        self, location: ViewportLocation, on_done: Callable[[], None] | None = None
    ) -> None:
        return None


def test_second_reconcile_supersedes_the_first_within_one_navigation() -> None:
    strategy = _Strategy()
    ctrl = PreviewScrollController(lambda: strategy)
    ctrl.arm(ScrollAnchor("doc", 5))

    revealed: list[str] = []
    ctrl.reconcile(lambda: revealed.append("reveal"))
    ctrl.reconcile()  # the background fill's re-anchor: no callback of its own

    assert len(strategy.calls) == 2, "both reconciles should reach the strategy"
    first_token, _ = strategy.calls[0]
    second_token, _ = strategy.calls[1]
    assert first_token != second_token, "each chain needs its own token to be cancellable"

    # The superseded chain landing late must NOT reveal — the newer chain owns it.
    _, first_cb = strategy.calls[0]
    assert first_cb is not None
    first_cb()
    assert revealed == [], "a superseded chain revealed before the newer scroll committed"

    # The current chain reveals, exactly once.
    _, second_cb = strategy.calls[1]
    assert second_cb is not None
    second_cb()
    assert revealed == ["reveal"]
    second_cb()
    assert revealed == ["reveal"], "reveal fired twice"


def test_a_new_navigation_still_fires_the_reveal_floor() -> None:
    """Superseded by a newer NAVIGATION is different from superseded by a newer
    chain: the reveal floor still applies, or a cancelled mount strands its
    container hidden."""
    strategy = _Strategy()
    ctrl = PreviewScrollController(lambda: strategy)
    ctrl.arm(ScrollAnchor("doc", 5))
    revealed: list[str] = []
    ctrl.reconcile(lambda: revealed.append("reveal"))

    ctrl.arm(ScrollAnchor("doc", 99))  # a newer navigation

    _, cb = strategy.calls[0]
    assert cb is not None
    cb()
    assert revealed == ["reveal"], "reveal floor was dropped for a superseded navigation"

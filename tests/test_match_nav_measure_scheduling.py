"""The ▲▼ markers must end up describing the viewport the preview is actually on.

They are a cache, refreshed by a coalesced, settle-gated poll. Both ways that
cache can be left describing a viewport the preview has left are pinned here:
a request arriving while a poll is in flight, and a rebuild landing on one.
"""

from __future__ import annotations

from typing import Any

import pytest

from fnd.tui.match_navigator import MatchNavigator


class _Nav(MatchNavigator):
    """A navigator with the poll and the region read replaced by hand-driven
    stand-ins, so the scheduling can be tested without a laid-out preview."""

    def __init__(self) -> None:
        self._above = 0
        self._below = 0
        self._measure_pending = False
        self._measure_again = False
        self._last_target: int | None = None
        self._refresh_gen = 0
        self.pending: list[Any] = []
        self.measured = 0

    def _poll_until_landed(  # type: ignore[override]
        self, retries: int, last_scroll: int | None, *, is_valid: Any, on_landed: Any
    ) -> None:
        self.pending.append(on_landed)

    def _measure_offscreen(self) -> None:  # type: ignore[override]
        self.measured += 1

    def land(self) -> None:
        """Fire the oldest in-flight poll, as a landed scroll would."""
        self.pending.pop(0)()


def test_a_request_during_an_in_flight_poll_still_measures() -> None:
    """The dropped request is the one that would have caught the real landing."""
    nav = _Nav()
    nav._schedule_measure()
    nav._schedule_measure()  # arrives while the first poll is still in flight
    nav.land()
    assert nav.measured == 1
    assert nav.pending, "the swallowed request never became a poll of its own"
    nav.land()
    assert nav.measured == 2


def test_a_burst_collapses_to_one_follow_up() -> None:
    """Coalescing still holds: many requests cost one extra poll, not many."""
    nav = _Nav()
    nav._schedule_measure()
    for _ in range(10):
        nav._schedule_measure()
    nav.land()
    assert len(nav.pending) == 1
    nav.land()
    assert nav.measured == 2
    assert not nav.pending, "the follow-up must not re-arm itself"


def test_a_rebuild_landing_on_a_poll_does_not_wedge_the_scheduler() -> None:
    """A superseded poll must release the in-flight flag; holding it made every
    later request return at the guard, so the markers stopped updating."""
    nav = _Nav()
    nav._schedule_measure()
    nav._refresh_gen += 1  # a rebuild supersedes the in-flight poll
    nav.land()
    assert nav.measured == 0, "a superseded poll must not measure"
    assert not nav._measure_pending, "the flag outlived the poll that set it"
    nav._schedule_measure()
    assert nav.pending, "a request after the rebuild was dropped as a duplicate"


def test_a_rebuild_clears_a_swallowed_request_too() -> None:
    nav = _Nav()
    nav._schedule_measure()
    nav._schedule_measure()
    nav._refresh_gen += 1
    nav.land()
    assert not nav.pending, "a superseded generation must not schedule a follow-up"
    assert not nav._measure_again


@pytest.mark.parametrize("rounds", [1, 3, 7])
def test_the_scheduler_always_settles(rounds: int) -> None:
    """Landing every poll must terminate — a follow-up that re-armed itself
    would spin the event loop for as long as the preview is open."""
    nav = _Nav()
    for _ in range(rounds):
        nav._schedule_measure()
    drained = 0
    while nav.pending and drained < 10:
        nav.land()
        drained += 1
    assert not nav.pending
    assert drained <= 2, f"a burst of {rounds} cost {drained} polls"

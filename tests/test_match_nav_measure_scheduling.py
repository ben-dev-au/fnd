"""The ▲▼ markers must end up describing the viewport the preview is actually on.

They are a cache, refreshed by a coalesced, settle-gated poll. Both ways that
cache can be left describing a viewport the preview has left are pinned here:
a request arriving while a poll is in flight, and a rebuild landing on one.
"""

from __future__ import annotations

from typing import Any

import pytest

from fnd.tui.match_navigator import MatchNavigator


class _App:
    """Just the timer facility the confirmation pass needs."""

    def __init__(self) -> None:
        self.timers: list[Any] = []

    def set_timer(self, delay: float, callback: Any, *, name: str = "") -> None:
        self.timers.append(callback)


class _Nav(MatchNavigator):
    """A navigator with the poll and the region read replaced by hand-driven
    stand-ins, so the scheduling can be tested without a laid-out preview.

    The real ``__init__`` runs against ``_App``: copying its fields here is what
    turned every new piece of navigator state into a failure in this file rather
    than in whatever forgot it.
    """

    def __init__(self) -> None:
        super().__init__(_App())  # type: ignore[arg-type]
        self.pending: list[Any] = []
        self.measured = 0
        # Successive values the region read yields, so a layout that is still
        # moving can be scripted; the last one repeats once exhausted.
        self.readings: list[tuple[int, int]] = []

    def _poll_until_landed(  # type: ignore[override]
        self, retries: int, last_scroll: int | None, *, is_valid: Any, on_landed: Any
    ) -> None:
        self.pending.append(on_landed)

    def _measure_offscreen(self) -> None:  # type: ignore[override]
        if self.readings:
            self._above, self._below = self.readings[0]
            if len(self.readings) > 1:
                self.readings.pop(0)
        self.measured += 1

    def land(self) -> None:
        """Fire the oldest in-flight poll, as a landed scroll would."""
        self.pending.pop(0)()

    def fire_timers(self) -> int:
        """Fire every armed confirmation timer; returns how many ran."""
        timers = list(self._app.timers)  # type: ignore[attr-defined]
        self._app.timers.clear()  # type: ignore[attr-defined]
        for cb in timers:
            cb()
        return len(timers)


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


def test_a_late_reflow_is_caught_by_a_confirmation_pass() -> None:
    """The counts come from LAYOUT, but every trigger is a scroll or a mount, so
    a reflow that lands after the last scroll would otherwise leave the border
    describing a layout that is gone — with nothing pending to notice."""
    nav = _Nav()
    nav._open_confirmation_window()
    nav.readings = [(0, 0), (0, 1), (0, 1)]
    nav._schedule_measure()
    nav.land()
    assert nav.measured == 1
    assert nav.fire_timers() == 1, "no confirmation was armed after the measure"
    nav.land()
    assert nav.measured == 2, "the confirmation did not re-measure"
    assert (nav.above, nav.below) == (0, 1), "the late reading was not adopted"


def test_confirmation_stops_as_soon_as_two_readings_agree() -> None:
    """Convergence, not a count: it keeps looking while the layout moves and
    stops the moment it holds still."""
    nav = _Nav()
    nav._open_confirmation_window()
    nav.readings = [(0, 0), (1, 0), (0, 1), (0, 1), (9, 9)]
    nav._schedule_measure()
    rounds = 0
    while nav.pending or nav._app.timers:  # type: ignore[attr-defined]
        nav.land() if nav.pending else nav.fire_timers()
        rounds += 1
        assert rounds < 20, "the confirmation pass never stopped"
    assert nav.measured == 4, f"expected to stop on the repeat, took {nav.measured} reads"
    assert (nav.above, nav.below) == (0, 1)


def test_confirmation_cannot_outlive_its_window() -> None:
    """A layout that never holds still must not keep the loop open."""
    nav = _Nav()
    nav._open_confirmation_window()
    nav._confirm_until = 0.0  # window already closed
    nav.readings = [(i, i) for i in range(20)]
    nav._schedule_measure()
    nav.land()
    assert not nav._app.timers, "a closed window still armed a confirmation"  # type: ignore[attr-defined]


def test_a_rebuild_cancels_pending_confirmations() -> None:
    nav = _Nav()
    nav._open_confirmation_window()
    nav.readings = [(0, 0), (1, 1)]
    nav._schedule_measure()
    nav.land()
    nav._refresh_gen += 1  # a new preview owns the measurement now
    nav.fire_timers()
    assert not nav.pending, "a confirmation from the previous preview still fired"

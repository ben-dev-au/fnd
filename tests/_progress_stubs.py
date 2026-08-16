"""Shared stand-ins for the progress line's collaborators.

One copy, because these encode a contract with ``ProgressFacility``: what it
reads off the widget and what it asks of the app. Four test modules had grown
their own near-identical versions, and the first time the facility started
reading ``content_size`` three of them broke at once — the duplication was
doing no work except deferring that breakage.
"""

from __future__ import annotations

from typing import Any


class StubSize:
    def __init__(self, width: int = 100) -> None:
        self.width = width


class StubBar:
    """The slice of :class:`FNDProgressBar` the facility touches."""

    def __init__(self, width: int = 100) -> None:
        self._fraction = 0.0
        self.label = ""
        self.visible = False
        self.content_size = StubSize(width)
        # Textual repaints on a reactive assignment, so counting the writes
        # counts the repaints.
        self.paints = 0

    @property
    def fraction(self) -> float:
        return self._fraction

    @fraction.setter
    def fraction(self, value: float) -> None:
        self.paints += 1
        self._fraction = value

    @property
    def is_idle(self) -> bool:
        return not self.visible

    def show(self) -> None:
        self.visible = True

    def hide(self) -> None:
        self.visible = False


class StubTimer:
    """Models Textual's ``Timer`` where it matters: ``stop()`` cancels the task
    and drops the reference, and ``_task`` is therefore the liveness signal.

    ``_active`` is deliberately not modelled — it is the PAUSE flag, and
    ``stop()`` actually *sets* it, so reading it as liveness yields a check
    that is always True. That mistake cost a debugging round; encoding the
    real shape here keeps it from being repeated.
    """

    def __init__(self) -> None:
        self.stopped = False
        self._task: object | None = object()

    def stop(self) -> None:
        self.stopped = True
        self._task = None

    def die(self) -> None:
        """A timer killed from outside, e.g. a cancelled task."""
        self._task = None


class StubProgressApp:
    """The slice of ``App`` the facility touches."""

    def __init__(self, bar: StubBar | None = None) -> None:
        self.bar = bar if bar is not None else StubBar()
        self.timers: list[StubTimer] = []
        self.watchdogs: list[tuple[StubTimer, Any]] = []

    def query_one(self, _selector: Any) -> StubBar:
        return self.bar

    def set_interval(self, _interval: float, _callback: Any, name: str = "") -> StubTimer:
        timer = StubTimer()
        self.timers.append(timer)
        return timer

    def set_timer(self, _delay: float, callback: Any, name: str = "") -> StubTimer:
        timer = StubTimer()
        self.watchdogs.append((timer, callback))
        return timer


class FakeClock:
    """Monotonic seconds under test control."""

    def __init__(self, now: float = 500.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

"""Progress sessions and the visibility policy that governs the line.

The line has one job the old strip failed at: from the moment an
operation starts until the moment it finishes, the user can see that
work is happening and roughly how far through it is. Four rules do most
of that:

* **No show delay.** A session paints on the frame it opens, so the
  feedback belongs to the keypress that caused it.
* **Minimum visible duration.** Work that finishes inside
  :data:`_MIN_VISIBLE_S` still shows a complete fill instead of a flash.
* **Completion is always painted.** A session eases to 1.0 and holds
  before clearing, so the line never vanishes part-filled.
* **Handoff continuity.** A session that supersedes another (holding
  Down through a result list) resumes from where the last one had got
  to rather than snapping back to zero.

Ownership: a session can only be closed by whoever holds it. The old
facility closed whatever happened to be active, so any stale teardown
path could retire a newer operation's bar — the "disappears after half a
second" failure.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fnd.tui.progress import calibration
from fnd.tui.progress.bar import FNDProgressBar
from fnd.tui.progress.model import OperationPlan, Phase, ProgressModel

if TYPE_CHECKING:
    from textual.app import App

# 20 Hz: one cell of a ~120-cell line every ~50 ms at a steady fill, which
# reads as motion without costing a repaint per frame. Runs only while a
# session is open.
_TICK_S = 1 / 20
_MIN_VISIBLE_S = 0.40
_COMPLETE_HOLD_S = 0.25
_COMPLETE_EASE_S = 0.12
# A new session starting within this long of the last one ending inherits
# its fill (see _HANDOFF_FLOOR_CAP).
_HANDOFF_S = 0.25
# ...but only up to here. A superseded operation's work part-counts towards
# its successor (warm caches, mounted DOM), so dropping to zero mid-sweep is
# a sawtooth; inheriting a near-complete fill would overstate a fresh start.
_HANDOFF_FLOOR_CAP = 0.5
# Backstop for a session whose owner died without closing it. Mirrors the
# preview pipeline's own watchdogs (see fnd/tui/preview/tuning.py) — bounded
# time, then repair, rather than a line stuck forever.
_HARD_CAP_S = 12.0

# The legacy determinate API (``open(phase, total=...)``) maps onto a plan of
# exactly one countable phase.
_SIMPLE_PHASE = "work"

Sampler = Callable[["ProgressSession"], bool]
"""Called once per tick. May advance the session; returns False when the
operation it is watching has finished."""


class ProgressSession:
    """Handle for one long-running operation. Use as a context manager."""

    def __init__(
        self,
        facility: ProgressFacility,
        *,
        model: ProgressModel,
        label: str = "",
        sampler: Sampler | None = None,
    ) -> None:
        self._facility = facility
        self._model = model
        self._label = label
        self._sampler = sampler
        self._closed = False
        self._total = 1
        self._progress = 0

    # ── state ────────────────────────────────────────────────────

    @property
    def operation_id(self) -> str:
        return self._model.plan.operation_id

    @property
    def phase(self) -> str:
        return self._model.phase.key

    @property
    def fraction(self) -> float:
        return self._model.fraction

    @property
    def label(self) -> str:
        return self._label

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def total(self) -> int:
        return self._total

    @property
    def progress(self) -> int:
        return self._progress

    # ── driving ──────────────────────────────────────────────────

    def enter(self, phase: str) -> None:
        if self._closed:
            return
        self._model.enter(phase)
        self._facility._render()

    def report(self, done: float, total: float) -> None:
        if self._closed:
            return
        self._model.report(done, total)
        self._facility._render()

    def set_label(self, label: str) -> None:
        """Set the trailing text. Empty clears it — the line carries no
        label unless it says something the user does not already know."""
        if self._closed or label == self._label:
            return
        self._label = label
        self._facility._render()

    def close(self) -> None:
        """Finish this operation. A no-op if a newer session took over —
        only the holder can retire its own bar."""
        if self._closed:
            return
        self._closed = True
        self._facility._on_close(self)

    def __enter__(self) -> ProgressSession:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    # ── legacy determinate API ───────────────────────────────────

    def set_total(self, total: int) -> None:
        self._total = max(1, total)
        self.report(self._progress, self._total)

    def set_progress(self, progress: int) -> None:
        self._progress = max(0, min(progress, self._total))
        self.report(self._progress, self._total)

    def advance(self, units: int = 1) -> None:
        self.set_progress(self._progress + units)

    def set_phase(self, label: str) -> None:
        self.set_label(label)


class ProgressFacility:
    """Owns the active session, the tick loop, and the visibility policy."""

    def __init__(
        self,
        app: App[Any],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._app = app
        self._clock = clock
        self._active: ProgressSession | None = None
        self._timer: Any = None
        self._shown_at = 0.0
        self._displayed = 0.0
        self._floor = 0.0
        # Set when a session closes; drives the ease-to-100% + hold.
        self._completing_at: float | None = None
        self._completing_from = 0.0
        self._completing_label = ""
        # Remembered across the handoff window.
        self._last_end = 0.0
        self._last_fraction = 0.0

    # ── public API ───────────────────────────────────────────────

    @property
    def active(self) -> ProgressSession | None:
        return self._active

    @property
    def displayed_fraction(self) -> float:
        return self._displayed

    def begin(
        self,
        plan: OperationPlan,
        *,
        label: str = "",
        sampler: Sampler | None = None,
    ) -> ProgressSession:
        """Start an operation. Paints immediately."""
        if self._active is not None and not self._active.closed:
            self._retire_active(superseded=True)

        now = self._clock()
        session = ProgressSession(
            self,
            model=ProgressModel(calibration.calibrated(plan), clock=self._clock),
            label=label,
            sampler=sampler,
        )
        self._active = session
        self._floor = self._handoff_floor(now)
        # Reset, don't carry: without this the previous operation's fill
        # would survive uncapped through the monotonic max in _render.
        self._displayed = self._floor
        self._shown_at = now
        self._completing_at = None
        self._start_ticking()
        self._render()
        return session

    def open(self, phase: str = "", *, total: int = 1) -> ProgressSession:
        """Legacy determinate entry point: one countable phase, driven by
        ``set_total`` / ``set_progress``."""
        plan = OperationPlan(
            operation_id="simple",
            phases=(Phase(key=_SIMPLE_PHASE, expected_ms=1000.0, countable=True),),
        )
        session = self.begin(plan, label=phase)
        session.set_total(total)
        return session

    def tick(self) -> None:
        """One frame of the visibility policy. Driven by the interval timer;
        called directly by tests."""
        now = self._clock()
        session = self._active
        if session is not None and not session.closed:
            if self._sample(session) is False:
                session.close()
                return
            if now - self._shown_at >= _HARD_CAP_S:
                session.close()
                return
            self._render()
            return
        if self._completing_at is not None:
            self._render_completing(now)

    def shutdown(self) -> None:
        """Stop the timer and flush learned durations. Called on app unmount."""
        self._stop_ticking()
        calibration.flush()

    # ── internals ────────────────────────────────────────────────

    def _sample(self, session: ProgressSession) -> bool | None:
        sampler = session._sampler
        if sampler is None:
            return None
        try:
            return sampler(session)
        except Exception:
            # An observer must never take the app down, and a broken one
            # should release the line rather than hold it forever.
            return False

    def _on_close(self, session: ProgressSession) -> None:
        if session is not self._active:
            # A stale holder retiring itself. Its bar already belongs to
            # someone else — leave it alone.
            return
        self._retire_active(superseded=False)

    def _retire_active(self, *, superseded: bool) -> None:
        session = self._active
        self._active = None
        if session is None:
            return
        session._closed = True
        now = self._clock()
        if superseded:
            # Abandoned work: its finished phases are still real measurements,
            # but the phase it died in has no end and is excluded by
            # observed_ms(). The successor takes the line over immediately, so
            # there is no completion animation — and it inherits the fill so a
            # held-down cursor doesn't saw the bar back to zero every keypress.
            calibration.record(session.operation_id, session._model.observed_ms())
            self._last_end = now
            self._last_fraction = self._displayed
            return
        # Closing the model ends the final phase, so calibration sees it.
        session._model.complete()
        calibration.record(session.operation_id, session._model.observed_ms())
        # A completed operation is not handed off: the next one is a new
        # activity and starts from zero.
        self._last_end = 0.0
        self._last_fraction = 0.0
        self._completing_at = now
        self._completing_from = self._displayed
        self._completing_label = session.label

    def _handoff_floor(self, now: float) -> float:
        if now - self._last_end > _HANDOFF_S:
            return 0.0
        return min(self._last_fraction, _HANDOFF_FLOOR_CAP)

    def _widget(self) -> FNDProgressBar | None:
        with contextlib.suppress(Exception):
            return self._app.query_one(FNDProgressBar)
        return None

    def _render(self) -> None:
        session = self._active
        if session is None:
            return
        self._displayed = max(self._displayed, self._floor, session._model.tick())
        self._paint(self._displayed, session.label, visible=True)

    def _render_completing(self, now: float) -> None:
        elapsed = now - (self._completing_at or now)
        ease = 1.0 if _COMPLETE_EASE_S <= 0 else min(1.0, elapsed / _COMPLETE_EASE_S)
        self._displayed = self._completing_from + (1.0 - self._completing_from) * ease
        # Hold a completed line until BOTH the minimum-visible budget and the
        # completion hold are spent — that is what turns a 40 ms cache hit
        # from a flash into a legible "done".
        min_visible_left = _MIN_VISIBLE_S - (now - self._shown_at)
        if elapsed >= _COMPLETE_HOLD_S and min_visible_left <= 0:
            self._clear()
            return
        self._paint(self._displayed, self._completing_label, visible=True)

    def _clear(self) -> None:
        self._completing_at = None
        self._displayed = 0.0
        self._floor = 0.0
        self._paint(0.0, "", visible=False)
        self._stop_ticking()

    def _paint(self, fraction: float, label: str, *, visible: bool) -> None:
        widget = self._widget()
        if widget is None:
            return
        widget.fraction = fraction
        widget.label = label
        if visible:
            widget.show()
        else:
            widget.hide()

    def _start_ticking(self) -> None:
        if self._timer is not None:
            return
        with contextlib.suppress(Exception):
            self._timer = self._app.set_interval(_TICK_S, self.tick, name="progress-tick")

    def _stop_ticking(self) -> None:
        if self._timer is None:
            return
        with contextlib.suppress(Exception):
            self._timer.stop()
        self._timer = None


__all__ = ["ProgressFacility", "ProgressSession", "Sampler"]

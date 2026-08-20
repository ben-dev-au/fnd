"""Progress sessions and the visibility policy that governs the line.

The line has one job the old strip failed at: from the moment an
operation starts until the moment it finishes, the user can see that
work is happening and roughly how far through it is. Four rules do most
of that:

* **No show delay.** A session paints on the frame it opens, so the
  feedback belongs to the keypress that caused it.
* **Minimum visible duration.** Work that finishes inside
  :data:`_MIN_VISIBLE_S` still shows a complete fill instead of a flash.
  Fast work is not hidden — a load the user can *see* complete is what
  makes the app feel fast.
* **Completion is always painted.** A session eases to 1.0 and holds
  before clearing, so the line never vanishes part-filled.
* **Handoff continuity.** A session that supersedes another (holding
  Down through a result list) resumes from where the last one had got
  to rather than snapping back to zero.

Ownership: a session can only be closed by whoever holds it. The old
facility closed whatever happened to be active, so any stale teardown
path could retire a newer operation's bar — the "disappears after half a
second" failure.

**Arbitration.** One line, two classes of work (see
:class:`~fnd.tui.progress.model.OperationKind`), so the facility keeps
two slots rather than one. An interactive operation always owns the
line; an ambient one is *suspended* while that happens and resumes
afterwards. Last-writer-wins over a single slot looked simpler and was
wrong: a background reindex runs for minutes and every navigation in
that window retired it permanently, so the run vanished from the line at
the user's first keypress and never came back.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from fnd.tui.progress import calibration
from fnd.tui.progress.bar import FNDProgressBar
from fnd.tui.progress.model import OperationKind, OperationPlan, Phase, ProgressModel

if TYPE_CHECKING:
    from textual.app import App

# 20 Hz: one cell of a ~120-cell line every ~50 ms at a steady fill, which
# reads as motion without costing a repaint per frame. Runs only while a
# session is open.
_TICK_S = 1 / 20
_MIN_VISIBLE_S = 0.40
# How long the full line is held before it clears. This is the "done" the
# user reads, so it has to survive a loop that may only tick a few times a
# second — hence a hold rather than an animation.
_COMPLETE_HOLD_S = 0.35
# A new session starting within this long of the last one ending inherits
# its fill (see _HANDOFF_FLOOR_CAP).
_HANDOFF_S = 0.25
# ...but only up to here. A superseded operation's work part-counts towards
# its successor (warm caches, mounted DOM), so dropping to zero mid-sweep is
# a sawtooth; inheriting a near-complete fill would overstate a fresh start.
_HANDOFF_FLOOR_CAP = 0.5
# How long the line may stay up without a SUBSTANTIVE update before it is
# retired: a phase change, a change in reported units, or a change of label.
#
# Deliberately not "since the last begin" — the paint check re-enters
# ``render_full_doc`` on a failed reveal and a burst of navigation supersedes
# constantly, so a per-session budget handed a stuck line a fresh one every
# time and it outlived any cap.
#
# And deliberately not "since the fill last moved". The eased fraction creeps
# for as long as a phase runs, so a stuck operation drifts a cell every second
# or two indefinitely — movement that comes from the model guessing, not from
# anything actually happening. Only real events count.
#
# Set well clear of the measured phase durations (the slowest observed is a
# ~1.4 s focus build) so a genuinely slow phase is never cut off mid-flight.
_STALL_CAP_S = 10.0
# Ambient work needs its own, far looser bound, and for a reason that is not
# just "it takes longer": its terminator is deterministic. The index tracker
# ends the session on ``task.done()``, a real asyncio result, where the
# preview tracker can only infer completion from heuristics. So this is a
# pure safety net rather than the normal path, and it is set beyond any
# plausible quiet stretch — a source walk over an evicted cloud vault can
# genuinely report nothing for minutes.
_AMBIENT_STALL_CAP_S = 600.0
# An active session paints at most this much. A full line is reserved for the
# completion animation, so "full" always means finished — and a stall while
# working is visibly distinct from a stall in the clear.
_ACTIVE_CEILING = 0.97
# Independent one-shot backstop, on its own timer, so a fault in the tick loop
# cannot leave a line on screen. Measured from the same instant as the stall
# cap, so it MUST sit clearly above it: at 8 s against a 10 s stall cap it beat
# the tick loop to every retirement, which made the stall path dead code and
# routed every case through _force_clear — a superseded retirement, so the
# operation lost both its completion animation and its calibration sample.
# The tick loop is the normal path; this only covers a tick loop that is gone.
_WATCHDOG_MARGIN_S = 5.0

# The legacy determinate API (``open(phase, total=...)``) maps onto a plan of
# exactly one countable phase.
_SIMPLE_PHASE = "work"


def _stall_cap(kind: OperationKind) -> float:
    return _AMBIENT_STALL_CAP_S if kind is OperationKind.AMBIENT else _STALL_CAP_S


Sampler = Callable[["ProgressSession"], bool]
"""Called once per tick. May advance the session; returns False when the
operation it is watching has finished."""


@runtime_checkable
class ProgressTracker(Protocol):
    """A subsystem's adapter onto the line.

    This is the one seam every subsystem shares. A tracker knows how to
    read its own pipeline and translate whatever it counts — rendered
    lines, mounted chunks, indexed files — into the plan's phases and a
    ``report(done, total)``; the facility knows nothing about any of them.
    That translation at the boundary is what lets a single line serve
    operations with no unit in common.

    ``begin`` is deliberately not part of the protocol: each subsystem
    needs its own arguments to choose a plan (the preview needs the file
    it is about to open). ``sample`` is the polymorphic half.
    """

    def sample(self, session: ProgressSession) -> bool: ...


class ProgressSession:
    """Handle for one long-running operation. Use as a context manager."""

    def __init__(
        self,
        facility: ProgressFacility,
        *,
        model: ProgressModel,
        label: str = "",
        sampler: Sampler | None = None,
        started_at: float = 0.0,
    ) -> None:
        self._facility = facility
        self._model = model
        self._label = label
        self._sampler = sampler
        self._closed = False
        self._total = 1
        self._progress = 0
        self._units: tuple[float, float] | None = None
        # Display state belongs to the session, not the facility: two
        # sessions can be alive at once, and a suspended ambient one has to
        # come back at the fill it had rather than at the fill the
        # navigation that interrupted it left behind.
        self._displayed = 0.0
        self._floor = 0.0
        self._shown_at = started_at
        self._moved_at = started_at

    # ── state ────────────────────────────────────────────────────

    @property
    def operation_id(self) -> str:
        return self._model.plan.operation_id

    @property
    def plan(self) -> OperationPlan:
        return self._model.plan

    @property
    def kind(self) -> OperationKind:
        return self._model.plan.kind

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
        if self._closed or phase == self.phase:
            return
        self._model.enter(phase)
        self._touch()

    def report(self, done: float, total: float) -> None:
        if self._closed:
            return
        units = (done, total)
        if units != self._units:
            self._units = units
            self._moved_at = self._facility.note_progress(self)
        self._model.report(done, total)
        self._facility.render(self)

    def set_label(self, label: str) -> None:
        """Set the trailing text. Empty clears it — the line carries no
        label unless it says something the user does not already know."""
        if self._closed or label == self._label:
            return
        self._label = label
        self._touch()

    def close(self) -> None:
        """Finish this operation. A no-op if a newer session took over —
        only the holder can retire its own bar."""
        if self._closed:
            return
        self._closed = True
        self._facility._on_close(self)

    def _touch(self) -> None:
        self._moved_at = self._facility.note_progress(self)
        self._facility.render(self)

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
    """Owns the sessions, the tick loop, and the visibility policy."""

    def __init__(
        self,
        app: App[Any],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._app = app
        self._clock = clock
        # Two slots, one line. See the module docstring on arbitration.
        self._active: ProgressSession | None = None
        self._ambient: ProgressSession | None = None
        self._timer: Any = None
        self._watchdog: Any = None
        # Last (cells, label, ambient, visible) actually handed to the widget,
        # so an unchanged frame costs nothing.
        self._rendered: tuple[int, str, bool, bool] | None = None
        self._warned_timer_probe = False
        self._painted = 0.0
        # Set when a session finishes; drives the full-line hold. While this
        # is set the hold owns the line, so nothing else may paint over it.
        self._completing_at: float | None = None
        self._completing_label = ""
        self._completing_ambient = False
        self._completing_shown_at = 0.0
        # Remembered across the handoff window.
        self._last_end = 0.0
        self._last_fraction = 0.0

    # ── public API ───────────────────────────────────────────────

    @property
    def active(self) -> ProgressSession | None:
        return self._active

    @property
    def ambient(self) -> ProgressSession | None:
        return self._ambient

    @property
    def displayed_fraction(self) -> float:
        return self._painted

    def displayed(self) -> ProgressSession | None:
        """The session the line is currently speaking for.

        Interactive first: an ambient run keeps ticking underneath but must
        never paint over the operation the user is waiting on. Nothing is
        displayed during a completion hold — that full line has been earned
        by the session that just finished.
        """
        if self._completing_at is not None:
            return None
        if self._active is not None and not self._active.closed:
            return self._active
        if self._ambient is not None and not self._ambient.closed:
            return self._ambient
        return None

    def _owns_line(self, session: ProgressSession) -> bool:
        """Whether ``session`` is the one the line is speaking for.

        Asked by slot rather than by :meth:`displayed`, because it is asked
        during retirement — and ``close()`` sets the closed flag before the
        facility has cleared the slot, so ``displayed`` would already have
        moved on. That made every completing session look undisplayed, and
        the line stopped painting its own completion.
        """
        if self._completing_at is not None:
            return False
        if session is self._active:
            return True
        if session is self._ambient:
            return self._active is None or self._active.closed
        return False

    def begin(
        self,
        plan: OperationPlan,
        *,
        label: str = "",
        sampler: Sampler | None = None,
    ) -> ProgressSession:
        """Start an operation. Paints immediately unless an interactive
        operation is already on the line and this one is ambient."""
        now = self._clock()
        ambient = plan.kind is OperationKind.AMBIENT
        previous = self._ambient if ambient else self._active
        was_idle = self.displayed() is None and self._completing_at is None
        # A re-dispatch inherits the stall budget of the session it replaces.
        # The paint check re-enters render_full_doc on a failed reveal, so a
        # stuck navigation begins a new session roughly every second; a fresh
        # budget each time is how a stalled line outlived every cap. The line
        # has been up and not moving, and replacing the holder does not change
        # that.
        inherited: float | None = None
        if previous is not None and not previous.closed:
            inherited = previous._moved_at
            self._retire(previous, superseded=True)

        session = ProgressSession(
            self,
            model=ProgressModel(calibration.calibrated(plan), clock=self._clock),
            label=label,
            sampler=sampler,
            started_at=now,
        )
        if ambient:
            self._ambient = session
        else:
            self._active = session
            session._floor = self._handoff_floor(now)
            # Reset, don't carry: without this the previous operation's fill
            # would survive uncapped through the monotonic max in render.
            session._displayed = session._floor
        # Starting a session only counts as progress when the line was
        # actually away. A session that SUPERSEDES one already on screen must
        # not refresh the stall budget: re-dispatch is exactly how a stuck
        # line kept buying more time.
        if was_idle:
            session._moved_at = self.note_progress(session)
        elif inherited is not None:
            session._moved_at = inherited
        if not ambient:
            self._completing_at = None
        self._start_ticking()
        self.render(session)
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
        called directly by tests.

        Nothing in here may raise. Textual's ``Timer._tick`` hands a callback
        exception to ``App._handle_exception``, which takes the whole app
        down — so an arithmetic slip in a progress bar would kill the session
        the progress bar exists to serve.
        """
        try:
            self._tick()
        except Exception as exc:  # the timer must survive anything
            self._log(f"progress tick failed: {exc!r}")

    def _tick(self) -> None:
        now = self._clock()
        # The hold comes first, and unconditionally: it is the only thing that
        # paints a full line, and letting a suspended ambient session resume
        # underneath it would cut the "done" the user is reading in half.
        if self._completing_at is not None:
            self._render_completing(now)
            return
        session = self.displayed()
        if session is not None:
            if self._sample(session) is False:
                session.close()
                return
            cap = _stall_cap(session.kind)
            if now - session._moved_at >= cap:
                self._log(
                    f"progress: nothing happened for {now - session._moved_at:.1f}s in "
                    f"{session.operation_id}/{session.phase} — retiring the line"
                )
                session.close()
                return
            self.render(session)
            return
        # Nothing to show: the line is idle, so the tick loop has no work.
        # Releasing it here also means a loop that somehow outlived its
        # session cannot spin forever.
        self._clear()

    def shutdown(self) -> None:
        """Stop the timers and flush learned durations. Called on app unmount.

        The flush is suppressed: it is the last thing to run on quit, and a
        failed write of a pacing hint must not surface as a crash on exit.
        """
        self._stop_ticking()
        self._disarm_watchdog()
        with contextlib.suppress(Exception):
            calibration.flush()

    def _log(self, message: str) -> None:
        with contextlib.suppress(Exception):
            self._app._diag_log(message)  # type: ignore[attr-defined]

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
        if session is not self._active and session is not self._ambient:
            # A stale holder retiring itself. Its bar already belongs to
            # someone else — leave it alone.
            return
        self._retire(session, superseded=False)

    def _retire(self, session: ProgressSession, *, superseded: bool) -> None:
        # Read this before clearing the slot.
        was_displayed = self._owns_line(session)
        if session is self._active:
            self._active = None
        elif session is self._ambient:
            self._ambient = None
        else:
            return
        session._closed = True
        now = self._clock()
        if superseded:
            # Abandoned work teaches nothing. Recording its finished phases
            # looked harmless — they are real measurements — but it feeds a
            # loop: an operation that gets stuck and re-dispatched writes long
            # durations, which raise the plan's expected total, which raises
            # the visibility cap, which lets the next stuck one stay up longer.
            # Only operations that actually completed get to set the pace.
            #
            # The successor takes the line over immediately, so there is no
            # completion animation — and it inherits the fill so a held-down
            # cursor doesn't saw the bar back to zero every keypress.
            if session.kind is OperationKind.INTERACTIVE:
                self._last_end = now
                self._last_fraction = session._displayed
            return
        # Closing the model ends the final phase, so calibration sees it.
        session._model.complete()
        calibration.record(session.operation_id, session._model.observed_ms())
        # A completed operation is not handed off: the next one is a new
        # activity and starts from zero.
        self._last_end = 0.0
        self._last_fraction = 0.0
        if not was_displayed:
            # An ambient run that finished while a navigation held the line.
            # Taking the line back to flash a full bar would interrupt the
            # thing the user is actually watching, and the indexer announces
            # its own completion by toast regardless.
            return
        self._completing_at = now
        self._completing_label = session.label
        self._completing_ambient = session.kind is OperationKind.AMBIENT
        self._completing_shown_at = session._shown_at
        # Paint the full line NOW, synchronously, rather than easing to it over
        # the next few ticks. The ease depended on the event loop being free —
        # and this loop is saturated during exactly the operations the line
        # reports on. Measured: at a 300ms tick gap (routine during a cold
        # navigation, where the loop blocks for 400-1274ms at a stretch) the
        # full frame was never painted at all, so the line vanished part-filled.
        # That is the "disappears without completing" the user reported.
        self._paint(1.0, session.label, ambient=self._completing_ambient, visible=True)
        self._start_ticking()

    def _handoff_floor(self, now: float) -> float:
        if now - self._last_end > _HANDOFF_S:
            return 0.0
        return min(self._last_fraction, _HANDOFF_FLOOR_CAP)

    def _widget(self) -> FNDProgressBar | None:
        with contextlib.suppress(Exception):
            return self._app.query_one(FNDProgressBar)
        return None

    def render(self, session: ProgressSession | None = None) -> None:
        """Paint the current frame for ``session``, if it owns the line."""
        if session is None:
            session = self.displayed()
        if session is None or session is not self.displayed():
            return
        # An ACTIVE session never paints a full line. A full line means "this
        # finished", and only the completion animation is entitled to say so —
        # otherwise a session whose phases have all eased out looks done while
        # it is still working, and a stall there is indistinguishable from a
        # stall in the clear.
        live = min(session._model.tick(), _ACTIVE_CEILING)
        session._displayed = max(session._displayed, session._floor, live)
        self._paint(
            session._displayed,
            session.label,
            ambient=session.kind is OperationKind.AMBIENT,
            visible=True,
        )

    def _render_completing(self, now: float) -> None:
        """Hold the full line, then clear it.

        The fill is already at 1.0 — painted synchronously when the session
        retired — so this only decides WHEN to put it away. Holding until both
        budgets are spent is what turns a 40 ms cache hit from a flash into a
        legible "done", which is the point: fast work the user can see finish
        is what makes the app feel fast.
        """
        elapsed = now - (self._completing_at or now)
        min_visible_left = _MIN_VISIBLE_S - (now - self._completing_shown_at)
        if elapsed >= _COMPLETE_HOLD_S and min_visible_left <= 0:
            self._completing_at = None
            self._resume_or_clear()
            return
        self._paint(1.0, self._completing_label, ambient=self._completing_ambient, visible=True)

    def _resume_or_clear(self) -> None:
        """Give the line back to a suspended ambient run, or put it away.

        Sampled before it repaints, so a background index that finished while
        a navigation held the line does not flash a stale bar on its way out.
        Retired as superseded rather than closed, deliberately: a run that
        ended unseen must not announce itself late, and it is the same rule
        as a run that ends while still suspended. The indexer's toast is what
        announces a background completion either way.
        """
        session = self.displayed()
        if session is None:
            self._clear()
            return
        if self._sample(session) is False:
            self._retire(session, superseded=True)
            self._clear()
            return
        # It was not being watched while it was suspended, so it has not had a
        # fair chance to show movement; do not charge it for that silence.
        session._moved_at = self._clock()
        session._shown_at = self._clock()
        self.render(session)

    def _clear(self) -> None:
        self._completing_at = None
        self._painted = 0.0
        self._paint(0.0, "", ambient=False, visible=False)
        self._stop_ticking()
        self._disarm_watchdog()

    def note_progress(self, session: ProgressSession | None = None) -> float:
        """Something real happened: a phase advanced, units changed, or the
        label changed. That is what earns the line more time — the eased fill
        moving on its own does not. Returns the timestamp to record."""
        now = self._clock()
        kind = session.kind if session is not None else OperationKind.INTERACTIVE
        self._arm_watchdog(_stall_cap(kind) + _WATCHDOG_MARGIN_S)
        return now

    def _paint(self, fraction: float, label: str, *, ambient: bool, visible: bool) -> None:
        self._painted = fraction if visible else 0.0
        widget = self._widget()
        if widget is None:
            return
        # Only touch the widget when the CELLS would differ. ``fraction`` is a
        # Textual reactive, and the eased value changes by a fraction of a
        # percent on every tick, so assigning it unconditionally repainted the
        # row 20 times a second to draw the identical thing — event-loop work
        # during exactly the navigation this line exists to make feel smoother.
        # Quantise against the width actually on screen.
        width = max(1, widget.content_size.width)
        rendered = (round(fraction * width), label, ambient, visible)
        if rendered == self._rendered:
            return
        self._rendered = rendered
        widget.fraction = fraction
        widget.label = label
        widget.ambient = ambient
        if visible:
            widget.show()
        else:
            widget.hide()

    def _start_ticking(self) -> None:
        """Ensure a live tick loop.

        Deliberately does NOT trust ``self._timer is not None``. A timer whose
        callback raised is stopped by Textual but still referenced here, and
        treating that as "already ticking" is what left the line frozen. Drop
        any timer that is no longer running and make a new one.
        """
        if self._timer is not None and self._timer_alive():
            return
        self._stop_ticking()
        with contextlib.suppress(Exception):
            self._timer = self._app.set_interval(_TICK_S, self.tick, name="progress-tick")

    def _timer_alive(self) -> bool:
        """Whether the stored timer is still running.

        ``Timer.stop()`` cancels its task and drops the reference, so ``_task``
        is the liveness signal. (``_active`` is NOT — that is the pause flag,
        and ``stop`` actually *sets* it.) Absent the attribute, assume alive:
        the watchdog is the guarantee here, not this check.
        """
        timer = self._timer
        if timer is None:
            return False
        if not hasattr(timer, "_task"):
            # A Textual upgrade renamed or removed the attribute. Degrade to
            # "assume alive" — the watchdog is the guarantee — but say so once,
            # because the degraded path is silent otherwise.
            if not self._warned_timer_probe:
                self._warned_timer_probe = True
                self._log("progress: Timer._task is gone; tick-loop liveness is now unchecked")
            return True
        return getattr(timer, "_task", None) is not None

    def _stop_ticking(self) -> None:
        if self._timer is None:
            return
        with contextlib.suppress(Exception):
            self._timer.stop()
        self._timer = None

    def _arm_watchdog(self, delay: float) -> None:
        """A one-shot clear that does not depend on the tick loop.

        Everything else here runs on the repeating timer, so any fault in that
        timer strands a visible line. This is a separate one-shot timer whose
        only job is to put the line away. Re-armed when the line MOVES, not
        when a session begins — see the note on _STALL_CAP_S. Its delay tracks
        the stall cap of whichever class of work is on the line, so an ambient
        run's much looser budget is not cut short by a backstop meant for a
        navigation.
        """
        self._disarm_watchdog()
        with contextlib.suppress(Exception):
            self._watchdog = self._app.set_timer(delay, self._force_clear, name="progress-watchdog")

    def _disarm_watchdog(self) -> None:
        if self._watchdog is None:
            return
        with contextlib.suppress(Exception):
            self._watchdog.stop()
        self._watchdog = None

    def _force_clear(self) -> None:
        """Last resort: the line is still up long after any real operation
        should have ended. Whatever held it, put it away."""
        self._watchdog = None
        widget = self._widget()
        if widget is None:
            # The line cannot be resolved right now — a modal screen on top of
            # the stack is the expected case, and an indexing run deliberately
            # sits behind one. Dropping the backstop here would retire it for
            # the rest of the session, because only real progress re-arms it
            # and a stalled session produces none by definition. Try again.
            self._arm_watchdog(_STALL_CAP_S + _WATCHDOG_MARGIN_S)
            return
        if widget.is_idle:
            return
        session = self.displayed()
        held = session.operation_id if session is not None else "(completing)"
        self._log(f"progress watchdog fired, line held by {held}")
        if session is not None:
            # Retire it as COMPLETED, not superseded: a superseded retirement
            # skips the completion entirely, so the line would vanish
            # part-filled — the exact symptom this whole backstop exists to
            # prevent. We have given up, but the user reads a line that
            # resolves; the diagnostic above records the truth.
            session.close()
        # close() paints the full line and leaves the hold to the tick loop —
        # but this backstop exists precisely for the case where that loop is
        # gone, so schedule the clear independently rather than trust it.
        with contextlib.suppress(Exception):
            self._app.set_timer(_COMPLETE_HOLD_S, self._clear, name="progress-final-clear")


__all__ = ["ProgressFacility", "ProgressSession", "ProgressTracker", "Sampler"]

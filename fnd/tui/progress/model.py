"""Phase-weighted progress model.

Pure — no Textual, no I/O, injected clock. An operation declares an
:class:`OperationPlan`: an ordered set of phases, each with an expected
duration. The model turns "which phase are we in, and how far through
it" into one monotonic 0..1 fraction.

A phase's **weight is its share of the plan's total expected duration**,
so a calibrated expectation reshapes the bar automatically — there are
no hand-tuned weights to drift out of step with what the code actually
does.

Phases with countable units (chunks mounted, files indexed) report a
real fraction. Phases with nothing to count — a single
``await build_done``, a layout settle — ease on elapsed time against
their expectation, so the line keeps moving through the waits that
dominate the budget without ever claiming a completion it has not
reached.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

# A timed phase asymptotes here rather than filling: only entering the next
# phase (or completing the operation) retires the remaining share, so a slow
# machine can't sit at a hard 100% mid-operation.
_PHASE_CEILING = 0.97
# Where a phase reads when it has run for exactly as long as expected.
#
# This single number balances the two halves of the original complaint. Too
# low and a correctly-estimated operation finishes with the bar partway (at
# 0.5 — a plain hyperbolic — the flat path completed at a median fill of
# 0.167). Too high and there is no headroom left for a phase that overruns, so
# the fill stops moving: at 0.9 a phase running 5x its estimate painted the
# same cell for 1.85s, which is the freeze this exists to prevent.
#
# 0.8 keeps an on-time phase reading nearly four fifths, and leaves enough
# range that a 5x overrun still advances a cell roughly every half second.
_ON_TIME = 0.8
# Guards a zero/negative expectation from dividing by zero.
_MIN_EXPECTED_MS = 1.0


class OperationKind(Enum):
    """Whether the user is standing there waiting for this.

    The distinction earns its place because the two classes have opposite
    needs and one line to share. INTERACTIVE work answers something the
    user just did, so it must take the line immediately. AMBIENT work runs
    on its own schedule — a background reindex started by auto-resume —
    and lasts orders of magnitude longer, so it must *yield* the line and
    get it back, rather than fight for it.
    """

    INTERACTIVE = "interactive"
    AMBIENT = "ambient"


@dataclass(frozen=True, slots=True)
class Phase:
    """One stage of an operation.

    ``expected_ms`` is a seed; :mod:`fnd.tui.progress.calibration` replaces it
    with what this machine actually measured. ``countable`` marks a phase whose
    caller can supply real units — it eases on time until the first
    :meth:`ProgressModel.report`.
    """

    key: str
    expected_ms: float
    countable: bool = False
    label: str | None = None


@dataclass(frozen=True, slots=True)
class OperationPlan:
    operation_id: str
    phases: tuple[Phase, ...]
    kind: OperationKind = OperationKind.INTERACTIVE

    def __post_init__(self) -> None:
        if not self.phases:
            raise ValueError(f"{self.operation_id}: a plan needs at least one phase")
        keys = [p.key for p in self.phases]
        if len(set(keys)) != len(keys):
            raise ValueError(f"{self.operation_id}: duplicate phase keys {keys}")

    def index_of(self, key: str) -> int:
        for i, phase in enumerate(self.phases):
            if phase.key == key:
                return i
        raise KeyError(f"{self.operation_id}: no phase {key!r}")

    def weights(self) -> tuple[float, ...]:
        """Each phase's share of the total expected duration."""
        expected = [max(_MIN_EXPECTED_MS, p.expected_ms) for p in self.phases]
        total = sum(expected)
        return tuple(e / total for e in expected)

    def recalibrated(self, expected_ms: dict[str, float]) -> OperationPlan:
        """A copy with measured expectations substituted where known."""
        return OperationPlan(
            operation_id=self.operation_id,
            phases=tuple(
                Phase(
                    key=p.key,
                    expected_ms=expected_ms.get(p.key, p.expected_ms),
                    countable=p.countable,
                    label=p.label,
                )
                for p in self.phases
            ),
            kind=self.kind,
        )


@dataclass(slots=True)
class _PhaseRun:
    started: float
    ended: float | None = None
    units: float | None = None


class ProgressModel:
    """Tracks one operation's position within its plan.

    The fraction is monotonic by construction: every recomputation is clamped
    against the highest value already shown, so no reordering of ``enter`` /
    ``report`` / ``tick`` can make the bar jump backwards.
    """

    def __init__(
        self,
        plan: OperationPlan,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._plan = plan
        self._weights = plan.weights()
        self._clock = clock
        now = clock()
        self._index = 0
        self._fraction = 0.0
        self._complete = False
        self._runs: dict[str, _PhaseRun] = {plan.phases[0].key: _PhaseRun(started=now)}

    # ── state ────────────────────────────────────────────────────

    @property
    def plan(self) -> OperationPlan:
        return self._plan

    @property
    def phase(self) -> Phase:
        return self._plan.phases[self._index]

    @property
    def fraction(self) -> float:
        return self._fraction

    @property
    def is_complete(self) -> bool:
        return self._complete

    # ── mutation ─────────────────────────────────────────────────

    def enter(self, key: str) -> None:
        """Advance to ``key``; every earlier phase counts finished.

        Re-entering the current phase, or naming an earlier one, is ignored —
        an operation only ever moves forwards.
        """
        if self._complete:
            return
        target = self._plan.index_of(key)
        if target <= self._index:
            return
        now = self._clock()
        current = self._runs.get(self.phase.key)
        if current is not None:
            current.ended = now
        self._index = target
        # Phases stepped over were never observed, so they get no run record —
        # calibration must not learn from a duration nobody measured.
        self._runs[key] = _PhaseRun(started=now)
        self.tick()

    def report(self, done: float, total: float) -> None:
        """Supply real units for the current phase."""
        if self._complete:
            return
        run = self._runs.get(self.phase.key)
        if run is None:
            return
        run.units = 0.0 if total <= 0 else max(0.0, min(1.0, done / total))
        self.tick()

    def tick(self) -> float:
        """Recompute the fraction against the current clock."""
        if self._complete:
            return self._fraction
        base = sum(self._weights[: self._index])
        weight = self._weights[self._index]
        self._fraction = max(self._fraction, base + weight * self._phase_fraction())
        return self._fraction

    def complete(self) -> None:
        if self._complete:
            return
        run = self._runs.get(self.phase.key)
        if run is not None and run.ended is None:
            run.ended = self._clock()
        self._complete = True
        self._fraction = 1.0

    # ── measurement ──────────────────────────────────────────────

    def observed_ms(self) -> dict[str, float]:
        """Measured duration per phase, for phases whose start AND end were
        both seen. Feeds :mod:`fnd.tui.progress.calibration`."""
        return {
            key: (run.ended - run.started) * 1000.0
            for key, run in self._runs.items()
            if run.ended is not None
        }

    def phase_elapsed_ms(self) -> float:
        run = self._runs.get(self.phase.key)
        if run is None:
            return 0.0
        end = run.ended if run.ended is not None else self._clock()
        return (end - run.started) * 1000.0

    # ── internals ────────────────────────────────────────────────

    def _phase_fraction(self) -> float:
        run = self._runs.get(self.phase.key)
        if run is not None and run.units is not None:
            return run.units
        expected = max(_MIN_EXPECTED_MS, self.phase.expected_ms)
        elapsed = self.phase_elapsed_ms()
        # Piecewise: proportional up to the expectation, asymptotic past it.
        #
        # A plain hyperbolic (elapsed / (elapsed + expected)) reads HALF the
        # phase at exactly its expected duration, so even a perfectly
        # calibrated operation finished with the bar at ~50% — measured on the
        # flat path, median fill at completion was 0.167. That is the
        # "it pauses partway" complaint, and no amount of fixing the
        # expectations cures it, because the curve itself is the cause.
        #
        # So: while the phase is running to time, the fill is proportional and
        # arrives at _ON_TIME exactly when the estimate says it should.
        # Past that the remaining headroom is consumed asymptotically, which is
        # what keeps an overrunning phase moving a cell at a time instead of
        # freezing (the failure an exponential tail produced).
        if elapsed <= expected:
            return _PHASE_CEILING * _ON_TIME * (elapsed / expected)
        overrun = _PHASE_CEILING - _PHASE_CEILING * _ON_TIME
        return _PHASE_CEILING * _ON_TIME + overrun * (1.0 - expected / elapsed)


__all__ = ["OperationKind", "OperationPlan", "Phase", "ProgressModel"]

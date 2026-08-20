"""Progress model: weights, easing, monotonicity, measurement.

Pure unit tests — no Textual, no app, no clock of their own. The model
takes an injected clock so every timing assertion here is exact rather
than a race against wall time.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from fnd.tui.progress.model import OperationPlan, Phase, ProgressModel


class FakeClock:
    """Monotonic seconds under test control."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance_ms(self, ms: float) -> None:
        self.now += ms / 1000.0


def plan(*pairs: tuple[str, float], countable: set[str] | None = None) -> OperationPlan:
    countable = countable or set()
    return OperationPlan(
        operation_id="test.op",
        phases=tuple(Phase(key=k, expected_ms=ms, countable=k in countable) for k, ms in pairs),
    )


# ── plan validation ──────────────────────────────────────────────


def test_plan_rejects_empty() -> None:
    with pytest.raises(ValueError, match="at least one phase"):
        OperationPlan(operation_id="x", phases=())


def test_plan_rejects_duplicate_keys() -> None:
    with pytest.raises(ValueError, match="duplicate phase keys"):
        plan(("a", 10.0), ("a", 20.0))


def test_weights_are_the_share_of_expected_duration() -> None:
    p = plan(("a", 100.0), ("b", 300.0))
    assert p.weights() == pytest.approx((0.25, 0.75))
    assert sum(p.weights()) == pytest.approx(1.0)


def test_recalibration_reshapes_the_weights() -> None:
    """The whole point of deriving weight from expected duration: a phase
    that turns out to dominate takes a proportionate share of the bar."""
    p = plan(("a", 100.0), ("b", 100.0))
    assert p.weights() == pytest.approx((0.5, 0.5))
    recal = p.recalibrated({"b": 900.0})
    assert recal.weights() == pytest.approx((0.1, 0.9))


def test_recalibration_keeps_seeds_for_unmeasured_phases() -> None:
    p = plan(("a", 100.0), ("b", 100.0))
    recal = p.recalibrated({"b": 300.0})
    assert recal.phases[0].expected_ms == 100.0
    assert recal.phases[1].expected_ms == 300.0


# ── easing ───────────────────────────────────────────────────────


def test_a_timed_phase_reads_nearly_full_when_it_runs_to_time() -> None:
    """The estimate coming true should look like it. An earlier curve read
    HALF the phase at its expected duration, so even a perfectly calibrated
    operation finished with the bar around 50% — measured on the flat path,
    a median fill at completion of 0.167. That is the "pauses partway"
    complaint, caused by the curve rather than by the estimates."""
    clock = FakeClock()
    m = ProgressModel(plan(("only", 200.0)), clock=clock)
    clock.advance_ms(200.0)
    assert m.tick() == pytest.approx(0.97 * 0.8, abs=1e-9)


def test_a_timed_phase_is_proportional_while_it_runs_to_time() -> None:
    clock = FakeClock()
    m = ProgressModel(plan(("only", 200.0)), clock=clock)
    clock.advance_ms(100.0)
    assert m.tick() == pytest.approx(0.97 * 0.8 * 0.5, abs=1e-9)


def test_a_timed_phase_keeps_a_fat_tail() -> None:
    """The property the curve was chosen for. Doubling and quadrupling the
    elapsed time must still move the bar by an amount a user can see — an
    exponential's tail is flat here, which froze the line for over a second
    on a phase that overran."""
    clock = FakeClock()
    m = ProgressModel(plan(("only", 100.0)), clock=clock)
    clock.advance_ms(200.0)
    at_2x = m.tick()
    clock.advance_ms(200.0)
    at_4x = m.tick()
    # Past the expectation the remaining headroom is consumed asymptotically:
    # always moving, never arriving.
    assert at_2x > 0.97 * 0.8
    assert at_4x > at_2x
    assert at_4x < 0.97


def test_timed_phase_never_completes_on_time_alone() -> None:
    clock = FakeClock()
    m = ProgressModel(plan(("only", 100.0)), clock=clock)
    clock.advance_ms(60_000.0)
    assert 0.96 < m.tick() < 0.97
    assert m.fraction < 1.0


def test_an_overrunning_phase_keeps_creeping() -> None:
    """The "nothing, nothing, nothing" case: a phase running many times its
    expectation must still advance on every tick. A clamped ceiling would
    freeze here; the scaled one cannot."""
    clock = FakeClock()
    m = ProgressModel(plan(("only", 100.0)), clock=clock)
    seen = [m.tick()]
    for _ in range(6):
        clock.advance_ms(100.0)
        seen.append(m.tick())
    assert all(b > a for a, b in pairwise(seen))
    assert seen[-1] < 0.97


def test_zero_expectation_does_not_divide_by_zero() -> None:
    clock = FakeClock()
    m = ProgressModel(plan(("only", 0.0)), clock=clock)
    clock.advance_ms(1.0)
    assert 0.0 < m.tick() < 0.97


# ── phase advance ────────────────────────────────────────────────


def test_entering_a_phase_retires_the_earlier_ones_in_full() -> None:
    clock = FakeClock()
    m = ProgressModel(plan(("a", 100.0), ("b", 100.0), ("c", 200.0)), clock=clock)
    m.enter("b")
    assert m.fraction == pytest.approx(0.25, abs=1e-9)
    m.enter("c")
    assert m.fraction == pytest.approx(0.5, abs=1e-9)


def test_skipping_a_phase_still_retires_it() -> None:
    clock = FakeClock()
    m = ProgressModel(plan(("a", 100.0), ("b", 100.0), ("c", 200.0)), clock=clock)
    m.enter("c")
    assert m.phase.key == "c"
    assert m.fraction == pytest.approx(0.5, abs=1e-9)


def test_progress_never_rewinds_on_a_stale_enter() -> None:
    clock = FakeClock()
    m = ProgressModel(plan(("a", 100.0), ("b", 100.0)), clock=clock)
    m.enter("b")
    before = m.fraction
    m.enter("a")
    assert m.phase.key == "b"
    assert m.fraction == before


def test_unknown_phase_key_is_a_programming_error() -> None:
    m = ProgressModel(plan(("a", 100.0)), clock=FakeClock())
    with pytest.raises(KeyError):
        m.enter("nope")


# ── countable phases ─────────────────────────────────────────────


def test_reported_units_override_the_easing() -> None:
    clock = FakeClock()
    m = ProgressModel(plan(("only", 1000.0), countable={"only"}), clock=clock)
    m.report(3, 4)
    assert m.tick() == pytest.approx(0.75)


def test_reported_units_are_clamped_to_the_phase() -> None:
    m = ProgressModel(plan(("only", 100.0), countable={"only"}), clock=FakeClock())
    m.report(99, 4)
    assert m.fraction == pytest.approx(1.0)


def test_zero_total_does_not_divide_by_zero() -> None:
    m = ProgressModel(plan(("a", 100.0), ("b", 100.0), countable={"a"}), clock=FakeClock())
    m.report(5, 0)
    assert m.fraction == pytest.approx(0.0)


def test_a_shrinking_report_cannot_pull_the_bar_back() -> None:
    """Root cause 2 in reverse: the mount window's denominator can be
    revised mid-flight, and the user must never see the bar retreat."""
    clock = FakeClock()
    m = ProgressModel(plan(("only", 100.0), countable={"only"}), clock=clock)
    m.report(9, 10)
    high = m.fraction
    m.report(1, 10)
    assert m.fraction == high


# ── completion ───────────────────────────────────────────────────


def test_complete_pins_the_fraction_to_one() -> None:
    m = ProgressModel(plan(("a", 100.0), ("b", 100.0)), clock=FakeClock())
    m.complete()
    assert m.fraction == 1.0
    assert m.is_complete


def test_a_completed_model_ignores_further_mutation() -> None:
    clock = FakeClock()
    m = ProgressModel(plan(("a", 100.0), ("b", 100.0)), clock=clock)
    m.complete()
    m.enter("b")
    m.report(0, 100)
    clock.advance_ms(5000.0)
    assert m.tick() == 1.0


# ── measurement for calibration ──────────────────────────────────


def test_observed_durations_are_recorded_per_phase() -> None:
    clock = FakeClock()
    m = ProgressModel(plan(("a", 100.0), ("b", 100.0)), clock=clock)
    clock.advance_ms(140.0)
    m.enter("b")
    clock.advance_ms(260.0)
    m.complete()
    observed = m.observed_ms()
    assert observed["a"] == pytest.approx(140.0)
    assert observed["b"] == pytest.approx(260.0)


def test_skipped_phases_are_not_reported_as_measured() -> None:
    """Calibration must never learn a duration nobody observed."""
    clock = FakeClock()
    m = ProgressModel(plan(("a", 100.0), ("b", 100.0), ("c", 100.0)), clock=clock)
    clock.advance_ms(50.0)
    m.enter("c")
    clock.advance_ms(50.0)
    m.complete()
    assert set(m.observed_ms()) == {"a", "c"}


def test_an_unfinished_phase_is_not_reported() -> None:
    clock = FakeClock()
    m = ProgressModel(plan(("a", 100.0), ("b", 100.0)), clock=clock)
    clock.advance_ms(50.0)
    m.enter("b")
    clock.advance_ms(50.0)
    assert set(m.observed_ms()) == {"a"}

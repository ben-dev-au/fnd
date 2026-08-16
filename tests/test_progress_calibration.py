"""Learned phase durations.

The calibrator is what stops the timed phases being a guess: the seeds
ship with the code, but after a few runs the bar is paced by what this
machine actually did. These tests pin the summarising rules and the
failure behaviour — telemetry must never break a caller.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from fnd.tui.progress import calibration
from fnd.tui.progress.model import OperationPlan, Phase

PLAN = OperationPlan(
    operation_id="op",
    phases=(
        Phase(key="a", expected_ms=100.0),
        Phase(key="b", expected_ms=100.0),
    ),
)


def test_no_history_means_the_seeds_stand() -> None:
    assert calibration.expected_ms("op") == {}
    assert calibration.calibrated(PLAN) is PLAN


def test_a_recorded_run_becomes_the_expectation() -> None:
    calibration.record("op", {"a": 250.0, "b": 750.0})
    assert calibration.expected_ms("op") == {"a": 250.0, "b": 750.0}
    recal = calibration.calibrated(PLAN)
    assert recal.weights() == pytest.approx((0.25, 0.75))


def test_the_median_rides_out_an_outlier() -> None:
    """A single cold-cache monster must not leave the bar crawling for
    every normal navigation afterwards — which a mean would do."""
    for ms in (100.0, 110.0, 90.0, 105.0, 8000.0):
        calibration.record("op", {"a": ms})
    assert calibration.expected_ms("op")["a"] == pytest.approx(105.0)


def test_only_recent_runs_count() -> None:
    for _ in range(12):
        calibration.record("op", {"a": 900.0})
    for _ in range(9):
        calibration.record("op", {"a": 100.0})
    assert calibration.expected_ms("op")["a"] == pytest.approx(100.0)


def test_operations_do_not_pool_their_samples() -> None:
    calibration.record("preview.cold", {"land": 900.0})
    calibration.record("preview.warm", {"land": 90.0})
    assert calibration.expected_ms("preview.cold") == {"land": 900.0}
    assert calibration.expected_ms("preview.warm") == {"land": 90.0}


def test_samples_below_the_noise_floor_are_dropped() -> None:
    calibration.record("op", {"a": 0.4, "b": 500.0})
    assert calibration.expected_ms("op") == {"b": 500.0}


def test_a_run_with_nothing_worth_recording_is_not_stored() -> None:
    calibration.record("op", {"a": 0.1})
    assert calibration.expected_ms("op") == {}


def test_history_survives_a_restart(isolated_progress_calibration: Path) -> None:
    calibration.record("op", {"a": 400.0})
    calibration.flush()
    assert isolated_progress_calibration.exists()
    calibration.reset_for_tests()
    assert calibration.expected_ms("op") == {"a": 400.0}


def test_a_corrupt_line_does_not_take_the_history_with_it(
    isolated_progress_calibration: Path,
) -> None:
    calibration.record("op", {"a": 400.0})
    calibration.flush()
    with isolated_progress_calibration.open("a", encoding="utf-8") as fh:
        fh.write("{not json at all\n")
        fh.write(json.dumps({"operation_id": "op", "phases": {"a": 600.0}}) + "\n")
    calibration.reset_for_tests()
    assert calibration.expected_ms("op")["a"] == pytest.approx(500.0)


def test_an_unwritable_location_is_not_a_crash(
    isolated_progress_calibration: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A calibration file we cannot write is a lost optimisation, never an
    error the user sees."""
    blocker = isolated_progress_calibration.parent / "blocker"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    monkeypatch.setattr("fnd.paths.progress_calibration_path", lambda: blocker / "x.jsonl")
    calibration.reset_for_tests()
    calibration.record("op", {"a": 400.0})
    calibration.flush()  # must not raise
    assert calibration.expected_ms("op")["a"] == pytest.approx(400.0)


def test_history_is_bounded(isolated_progress_calibration: Path) -> None:
    for i in range(400):
        calibration.record("op", {"a": float(100 + i)})
    calibration.flush()
    lines = isolated_progress_calibration.read_text(encoding="utf-8").splitlines()
    assert 0 < len(lines) <= 200


def test_a_non_finite_duration_is_ignored(isolated_progress_calibration: Path) -> None:
    """``json.loads`` accepts ``Infinity`` and ``NaN``, and an infinite value
    sails past a bare ``>=`` filter. It then reaches ``OperationPlan.weights``,
    where ``inf / inf`` is ``nan`` and the bar stops advancing entirely — a
    corrupt line would freeze the line rather than be skipped.

    Written after the guard was added to ``record`` but silently not to the
    loader, which is the half this test covers.
    """
    calibration.record("op", {"a": 400.0})
    calibration.flush()
    with isolated_progress_calibration.open("a", encoding="utf-8") as fh:
        fh.write('{"operation_id": "op", "phases": {"a": Infinity, "b": 500.0}}\n')
        fh.write('{"operation_id": "op", "phases": {"a": NaN}}\n')
    calibration.reset_for_tests()

    expected = calibration.expected_ms("op")
    assert math.isfinite(expected["a"])
    assert expected["a"] == pytest.approx(400.0)
    # A bad value drops that phase, not the whole record.
    assert expected["b"] == pytest.approx(500.0)

    plan = OperationPlan(
        operation_id="op",
        phases=(Phase(key="a", expected_ms=100.0), Phase(key="b", expected_ms=100.0)),
    )
    assert all(math.isfinite(w) for w in calibration.calibrated(plan).weights())


def test_a_non_finite_observation_is_never_recorded() -> None:
    calibration.record("op", {"a": float("inf"), "b": float("nan"), "c": 300.0})
    assert calibration.expected_ms("op") == {"c": 300.0}

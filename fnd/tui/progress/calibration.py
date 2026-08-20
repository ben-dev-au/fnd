"""Learned phase durations for the progress line.

Mirrors :mod:`fnd.tui.cost_estimate`: append one record per completed
operation, summarise the recent ones, fall back to the plan's seed when
nothing has been recorded. That is what turns a timed phase from a guess
into a pace matched to this machine and this corpus.

Two departures from the indexer's calibrator, both deliberate:

* **Median, not mean.** Navigation durations are heavy-tailed — one 8 s
  outlier (a cold monster PDF, a machine under load) would drag a
  five-sample mean far off the typical case and leave the bar crawling
  for every normal navigation afterwards.
* **No writing during interaction.** An operation completes several times
  a second under a held cursor key, and every completion records a sample.
  Writing there would put file I/O on the event loop in the middle of the
  very navigation the line exists to make feel better. The history lives in
  memory and is written by :func:`flush`, which the app calls on unmount.

Telemetry must never break a caller: every filesystem and parse path
here is suppressed, and a failure just means the seeds stay in use.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import statistics
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

from fnd import paths
from fnd.tui.progress.model import OperationPlan

# How many recent runs of an operation to summarise. Wider than the
# indexer's 5 because these samples are individually noisy — a median over
# 9 rides out a couple of outliers without going stale.
_SAMPLE_SIZE = 9
# Total records kept on disk across all operations.
_MAX_HISTORY = 200
# A run this short tells us nothing useful and would drag the expectation
# down for the cases the bar actually exists to cover.
_MIN_SAMPLE_MS = 5.0


@dataclass(frozen=True)
class PhaseRecord:
    """One completed operation's measured phase durations."""

    operation_id: str
    completed_at: float  # unix timestamp
    phases: dict[str, float]  # phase key -> milliseconds


@dataclass
class _Store:
    records: list[PhaseRecord] = field(default_factory=list)
    loaded: bool = False
    dirty: bool = False


_store = _Store()


def _usable(ms: float) -> bool:
    """Whether a duration is worth learning from.

    ``json.loads`` accepts ``Infinity`` and ``NaN``, and an infinite value
    passes a bare ``>=`` comparison. It then reaches ``OperationPlan.weights``,
    where ``inf / inf`` is ``nan`` and the fraction stops advancing at all — a
    corrupt line in this file would freeze the bar rather than be ignored, and
    corrupt lines are already an expected case here.
    """
    return math.isfinite(ms) and ms >= _MIN_SAMPLE_MS


def _path() -> Path:
    return paths.progress_calibration_path()


def _load() -> None:
    if _store.loaded:
        return
    _store.loaded = True
    path = _path()
    if not path.exists():
        return
    with contextlib.suppress(OSError), path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            # KeyError too: a line can be valid JSON and still lack "phases"
            # or "operation_id", and KeyError is a subclass of none of the
            # others. It escaped _load, which runs from ProgressFacility.begin
            # — and SearchController.run does not guard that call, so one
            # corrupt history line broke the next search. The module docstring
            # promises every parse path here is suppressed; it now is.
            with contextlib.suppress(json.JSONDecodeError, TypeError, ValueError, KeyError):
                data = json.loads(raw)
                phases = {
                    str(k): float(v)
                    for k, v in dict(data.get("phases") or {}).items()
                    if _usable(float(v))
                }
                if phases:
                    _store.records.append(
                        PhaseRecord(
                            operation_id=str(data["operation_id"]),
                            completed_at=float(data.get("completed_at", 0.0)),
                            phases=phases,
                        )
                    )


def record(operation_id: str, observed_ms: Mapping[str, float]) -> None:
    """Remember one completed operation. Phases below the noise floor are
    dropped rather than recorded as near-zero."""
    phases = {k: float(v) for k, v in observed_ms.items() if _usable(float(v))}
    if not phases:
        return
    _load()
    _store.records.append(
        PhaseRecord(operation_id=operation_id, completed_at=time.time(), phases=phases)
    )
    del _store.records[:-_MAX_HISTORY]
    _store.dirty = True


def expected_ms(operation_id: str) -> dict[str, float]:
    """Median observed duration per phase over the recent runs of
    ``operation_id``. Phases never observed are absent — the caller keeps
    the plan's seed for those."""
    _load()
    recent = [r for r in _store.records if r.operation_id == operation_id][-_SAMPLE_SIZE:]
    samples: dict[str, list[float]] = {}
    for rec in recent:
        for key, ms in rec.phases.items():
            samples.setdefault(key, []).append(ms)
    return {key: statistics.median(values) for key, values in samples.items() if values}


def calibrated(plan: OperationPlan) -> OperationPlan:
    """``plan`` with measured expectations substituted where available."""
    measured = expected_ms(plan.operation_id)
    return plan.recalibrated(measured) if measured else plan


def flush() -> None:
    """Persist the history. Safe to call at any time; a no-op when clean."""
    if not _store.dirty:
        return
    path = _path()
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        # Temp file + os.replace: a crash mid-write leaves the previous
        # history intact rather than truncated. Same reasoning as
        # cost_estimate.record_run.
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            for entry in _store.records:
                fh.write(json.dumps(asdict(entry)) + "\n")
        os.replace(tmp_path, path)
        # Only now: clearing it up front meant a failed write left the store
        # marked clean, so nothing was ever retried once storage recovered.
        _store.dirty = False


def reset_for_tests() -> None:
    """Drop the in-memory history so a test can start from the seeds."""
    _store.records.clear()
    _store.loaded = False
    _store.dirty = False


__all__ = [
    "PhaseRecord",
    "calibrated",
    "expected_ms",
    "flush",
    "record",
    "reset_for_tests",
]

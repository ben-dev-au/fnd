"""Per-(collection, file) failure log used to populate the still-flat
drill-in screen.

When an Update index run skips a file (ExtractError, iCloud-offloaded,
worker stall, BrokenProcessPool), the runner records the path + reason
+ timestamp here. The drill-in screen reads the latest record per file
so the user can see why each still-flat PDF is still flat and retry
texturising it.

Records older than ``_TTL_DAYS`` are pruned on every save. A run that
ends without recording a failure for a file effectively clears the
stale record on next prune; we don't actively delete on success to
keep the writer hot-path cheap."""

from __future__ import annotations

import contextlib
import datetime as dt
import tomllib
from dataclasses import dataclass
from pathlib import Path

import tomli_w

from fnd import paths

_TTL_DAYS = 30


def _log_path() -> Path:
    return paths.failure_log_path()


@dataclass(slots=True, frozen=True)
class FailureRecord:
    collection: str
    path: str
    reason: str
    recorded_at: str  # ISO-8601 UTC


def _load_records() -> list[FailureRecord]:
    path = _log_path()
    if not path.exists():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    rows = data.get("failures", [])
    if not isinstance(rows, list):
        return []
    out: list[FailureRecord] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            FailureRecord(
                collection=str(row.get("collection", "")),
                path=str(row.get("path", "")),
                reason=str(row.get("reason", "")),
                recorded_at=str(row.get("recorded_at", "")),
            )
        )
    return out


def _save_records(records: list[FailureRecord]) -> None:
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "failures": [
            {
                "collection": r.collection,
                "path": r.path,
                "reason": r.reason,
                "recorded_at": r.recorded_at,
            }
            for r in records
        ]
    }
    with contextlib.suppress(OSError):
        path.write_text(tomli_w.dumps(payload), encoding="utf-8")


def _prune(records: list[FailureRecord]) -> list[FailureRecord]:
    """Drop records older than ``_TTL_DAYS`` and dedupe to the most-recent
    record per (collection, path)."""
    cutoff = dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=_TTL_DAYS)
    fresh: dict[tuple[str, str], FailureRecord] = {}
    for r in records:
        try:
            stamp = dt.datetime.fromisoformat(r.recorded_at)
        except ValueError:
            continue
        if stamp < cutoff:
            continue
        key = (r.collection, r.path)
        existing = fresh.get(key)
        if existing is None or r.recorded_at > existing.recorded_at:
            fresh[key] = r
    return list(fresh.values())


def record_failure(*, collection: str, path: str, reason: str) -> None:
    """Append one failure record + prune. Safe to call on any thread."""
    records = _load_records()
    records.append(
        FailureRecord(
            collection=collection,
            path=path,
            reason=reason,
            recorded_at=dt.datetime.now(tz=dt.UTC).isoformat(timespec="seconds"),
        )
    )
    _save_records(_prune(records))


def list_failures(*, collection: str | None = None) -> list[FailureRecord]:
    """Return the freshest failure per (collection, path), optionally
    scoped to one collection."""
    records = _prune(_load_records())
    if collection is None:
        return records
    return [r for r in records if r.collection == collection]


def clear_failure(*, collection: str, path: str) -> None:
    """Forget a single record - call after a successful retry."""
    records = [r for r in _load_records() if not (r.collection == collection and r.path == path)]
    _save_records(records)


__all__ = [
    "FailureRecord",
    "clear_failure",
    "list_failures",
    "record_failure",
]

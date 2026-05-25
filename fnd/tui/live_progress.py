"""Per-extraction live-progress shared state.

The PDF worker subprocess emits ("page", N) / ("total", N) heartbeats
as it processes pages. The runner forwards each beat into this module
so the IndexerScreen's 1Hz timer can surface per-page progress and
a session-wide "avg seconds per page" stat without waiting for a
file_complete event.

Single-extraction-at-a-time is a hard constraint of the worker pool
(max_workers=1), so a single shared snapshot is sufficient. Thread-
safe because the runner's heartbeat callback fires on the
stall-detection polling thread while the modal tick reads from the
asyncio loop."""

from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass

_lock = threading.Lock()


@dataclass(slots=True)
class _State:
    path: str = ""
    pages_done: int = 0
    pages_total: int = 0
    file_started_monotonic: float = 0.0
    # Inter-page-event timing for the current file; the previous
    # "page" or "file-start" beat's monotonic timestamp. Used to
    # measure how long each page actually took so the session average
    # reflects real per-page extraction cost.
    last_beat_monotonic: float = 0.0
    # Session counters: how many page beats observed and the summed
    # inter-beat seconds across the run. avg = secs / pages.
    session_pages: int = 0
    session_page_seconds: float = 0.0


_state = _State()


def report_heartbeat(beat: object) -> None:
    """Record one heartbeat from the worker. Idempotent / forgiving:
    unknown beat shapes are dropped silently."""
    if not isinstance(beat, tuple) or len(beat) != 2:
        return
    tag, value = beat
    now = time.monotonic()
    with _lock:
        if tag == "file-start":
            _state.path = str(value)
            _state.pages_done = 0
            _state.pages_total = 0
            _state.file_started_monotonic = now
            _state.last_beat_monotonic = now
        elif tag == "total":
            with contextlib.suppress(TypeError, ValueError):
                _state.pages_total = int(value)
        elif tag == "page":
            with contextlib.suppress(TypeError, ValueError):
                _state.pages_done = int(value) + 1
                if _state.last_beat_monotonic > 0.0:
                    _state.session_page_seconds += now - _state.last_beat_monotonic
                    _state.session_pages += 1
                _state.last_beat_monotonic = now


def snapshot() -> tuple[str, int, int, float]:
    """Return (path, pages_done, pages_total, file_started_monotonic)
    for whatever extraction is currently in flight. All zero / empty
    when nothing is in flight."""
    with _lock:
        return (
            _state.path,
            _state.pages_done,
            _state.pages_total,
            _state.file_started_monotonic,
        )


def session_snapshot() -> tuple[int, float]:
    """Return (session_pages, session_page_seconds). Divide to get
    avg seconds per page across this run."""
    with _lock:
        return (_state.session_pages, _state.session_page_seconds)


def seconds_since_last_beat() -> float:
    """Seconds since the last per-page or file-start beat for the
    current file. Returns 0.0 when no extraction is in flight so
    the caller can use it as a "no-stuck-warning" sentinel."""
    with _lock:
        if _state.last_beat_monotonic == 0.0:
            return 0.0
        return max(0.0, time.monotonic() - _state.last_beat_monotonic)


def reset() -> None:
    """Clear in-flight state only. Called by the runner between files
    so a stale snapshot from a previous file can't bleed into the
    next file's page progress. Session counters survive so the
    avg-per-page stat accumulates across the whole run."""
    with _lock:
        _state.path = ""
        _state.pages_done = 0
        _state.pages_total = 0
        _state.file_started_monotonic = 0.0
        _state.last_beat_monotonic = 0.0


def reset_session() -> None:
    """Clear both in-flight and session-wide counters. Called once at
    the start of a chain so averages from a previous run don't bleed
    into a fresh one."""
    with _lock:
        _state.path = ""
        _state.pages_done = 0
        _state.pages_total = 0
        _state.file_started_monotonic = 0.0
        _state.last_beat_monotonic = 0.0
        _state.session_pages = 0
        _state.session_page_seconds = 0.0


__all__ = [
    "report_heartbeat",
    "reset",
    "reset_session",
    "seconds_since_last_beat",
    "session_snapshot",
    "snapshot",
]

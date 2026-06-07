"""Background-computed cache for the flat-PDF inventory scan.

``settings_screen._flat_pdfs_with_reasons`` walks every collection
source on disk and diffs it against the tantivy index. On a real corpus
that costs seconds (measured 2-3 s; 20-30 s mid-rebuild, when the scan
contends with the index the runner is rewriting). Run on the UI thread
it freezes the whole TUI until it returns — the recurring "portal takes
forever to open" stall.

Every UI consumer reads through this module instead of calling the scan
directly. Cached results return instantly (safe on the event loop); a
stale or missing entry schedules a daemon-thread recompute whose result
is delivered to ``on_ready`` marshalled back onto the UI thread. The
event loop never runs the scan itself, so it can never freeze.

Mirrors the threading contract of :mod:`fnd.tui.lazy_trailing`; kept
separate because that module is typed to ``str`` trailing values and
hard-wires a settings-screen repaint, whereas this returns the full row
list and lets each caller supply its own ready callback.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fnd.tui.app import FNDApp

# (collection, path, reason, recorded_at) — the shape returned by
# settings_screen._flat_pdfs_with_reasons.
Row = tuple[str, str, str, str | None]

# Keyed by collection scope (None = all collections).
_CACHE: dict[str | None, tuple[list[Row], float]] = {}
# Callbacks waiting on an in-flight compute for each scope.
_PENDING: dict[str | None, list[Callable[[list[Row]], None]]] = {}
_LOCK = threading.Lock()
_TTL_SECONDS = 5.0


def cached_rows(collection: str | None = None) -> list[Row] | None:
    """Last computed rows for ``collection``, or None if never computed.
    Instant — safe to call from the render path / event loop."""
    with _LOCK:
        entry = _CACHE.get(collection)
        return entry[0] if entry is not None else None


def cached_count(collection: str | None = None) -> int | None:
    """Count of flat PDFs from the last scan, or None if never computed."""
    rows = cached_rows(collection)
    return None if rows is None else len(rows)


def is_fresh(collection: str | None = None, *, ttl: float = _TTL_SECONDS) -> bool:
    """True when a cached scan exists and is younger than ``ttl``."""
    with _LOCK:
        entry = _CACHE.get(collection)
        return entry is not None and (time.monotonic() - entry[1]) < ttl


def schedule_refresh(
    app: FNDApp | Any,
    collection: str | None = None,
    *,
    on_ready: Callable[[list[Row]], None] | None = None,
    ttl: float = _TTL_SECONDS,
    force: bool = False,
) -> None:
    """Ensure a (re)scan of ``collection`` runs off the event loop.

    Returns immediately. When a fresh cache entry already exists and
    ``force`` is False, ``on_ready`` is invoked inline with it (the
    caller is on the UI thread). Otherwise the scan runs in a daemon
    thread and ``on_ready`` fires when it completes, marshalled back
    onto the UI thread via ``app.call_from_thread``. Concurrent
    schedules for the same scope share a single worker; every caller's
    ``on_ready`` is notified.
    """
    fresh_rows: list[Row] | None = None
    with _LOCK:
        entry = _CACHE.get(collection)
        if not force and entry is not None and (time.monotonic() - entry[1]) < ttl:
            fresh_rows = entry[0]
        else:
            already_running = collection in _PENDING
            waiters = _PENDING.setdefault(collection, [])
            if on_ready is not None:
                waiters.append(on_ready)
            if already_running:
                # A worker is already computing this scope; it will call
                # our callback when it finishes.
                return
    if fresh_rows is not None:
        if on_ready is not None:
            on_ready(fresh_rows)
        return

    def _worker() -> None:
        try:
            from fnd.tui.settings_screen import _flat_pdfs_with_reasons

            rows = list(_flat_pdfs_with_reasons(collection=collection))
        except Exception:
            # Keep any prior value rather than wiping the count to 0 on a
            # transient scan failure (e.g. index locked mid-rebuild).
            rows = cached_rows(collection) or []
        with _LOCK:
            _CACHE[collection] = (rows, time.monotonic())
            waiters = _PENDING.pop(collection, [])
        for cb in waiters:
            _deliver(app, cb, rows)

    threading.Thread(target=_worker, daemon=True).start()


def _deliver(app: FNDApp | Any, cb: Callable[[list[Row]], None], rows: list[Row]) -> None:
    """Marshal one callback onto the UI thread. Under test runs the
    worker can finish before the loop is up; close any returned coroutine
    so it isn't reported as never-awaited. App teardown / screen
    reshuffle is benign — the cached value is read on the next render."""
    import asyncio

    with contextlib.suppress(Exception):
        result = app.call_from_thread(cb, rows)
        if asyncio.iscoroutine(result):
            result.close()


def invalidate(collection: str | None = None) -> None:
    """Drop the cached scan for ``collection`` so the next schedule
    recomputes (e.g. after a chain finishes or a row is dismissed)."""
    with _LOCK:
        _CACHE.pop(collection, None)


def invalidate_all() -> None:
    """Wipe the whole cache. Used by tests."""
    with _LOCK:
        _CACHE.clear()


__all__ = [
    "Row",
    "cached_count",
    "cached_rows",
    "invalidate",
    "invalidate_all",
    "is_fresh",
    "schedule_refresh",
]

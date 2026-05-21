"""Background-computed trailing values for settings rows.

A row's ``value_getter`` runs synchronously inside the render path. When
that getter walks the filesystem (cache size, extra disk usage, etc.)
the first render blocks until the walk finishes — the user sees an
empty trailing column for ~200 ms.

Solution: rows that touch the filesystem wrap their compute callable
in :func:`get_or_schedule`. The wrapper returns a placeholder (``…``)
immediately and starts the real work in a background thread. When the
thread completes, it caches the value and re-renders the settings
screen via ``app.call_from_thread``.

The cache has a TTL so long-lived screens see fresh data; screens
also invalidate on resume so reopening always recomputes.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fnd.tui.app import FNDApp

_CACHE: dict[str, tuple[str, float]] = {}
_PENDING: set[str] = set()
_LOCK = threading.Lock()
_TTL_SECONDS = 30.0

PLACEHOLDER = "…"


def get_or_schedule(
    app: FNDApp | Any,
    key: str,
    compute: Callable[[], str],
    *,
    ttl: float = _TTL_SECONDS,
) -> str:
    """Return cached value if fresh; otherwise schedule background work
    and return :data:`PLACEHOLDER`.

    When the background thread completes it writes to the cache and
    asks the app to refresh its current settings screen so the value
    becomes visible.

    Safe to call from the render path.
    """
    now = time.monotonic()
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None and (now - cached[1]) < ttl:
            return cached[0]
        if key in _PENDING:
            return PLACEHOLDER
        _PENDING.add(key)

    def _worker() -> None:
        try:
            value = compute()
        except Exception:
            value = ""
        with _LOCK:
            _CACHE[key] = (value, time.monotonic())
            _PENDING.discard(key)
        # Refresh whatever settings screen is on top so the new value
        # paints. ``call_from_thread`` is the Textual hook for marshalling
        # work back onto the UI thread. Under test runs the worker can
        # complete before the event loop is fully up; close the returned
        # coroutine in that case to silence the "never awaited" warning.
        # App may be shutting down or the screen stack reshuffled — safe
        # to ignore; next render will pick up the cached value.
        import asyncio

        with contextlib.suppress(Exception):
            result = app.call_from_thread(_refresh_active_settings, app)
            if asyncio.iscoroutine(result):
                result.close()

    threading.Thread(target=_worker, daemon=True).start()
    return PLACEHOLDER


def invalidate(key: str) -> None:
    """Drop the cached value for ``key`` so the next read recomputes."""
    with _LOCK:
        _CACHE.pop(key, None)


def invalidate_all() -> None:
    """Wipe the whole cache. Used by tests."""
    with _LOCK:
        _CACHE.clear()


def _refresh_active_settings(app: FNDApp | Any) -> None:
    """Re-render the active settings screen, if any."""
    try:
        from fnd.tui.settings_screen import SettingsList, SettingsScreen

        screen = app.screen
        if isinstance(screen, SettingsScreen):
            screen.query_one(SettingsList).refresh_values()
    except Exception:
        # Screen may have been popped between scheduling and completion.
        pass


__all__ = ["PLACEHOLDER", "get_or_schedule", "invalidate", "invalidate_all"]

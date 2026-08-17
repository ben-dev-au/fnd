"""Report when the event loop stops answering, and say what was running.

A freeze is an event loop that does not come back, and the hard part is never
noticing one — it is knowing which piece of work held it. Guessing has a poor
record here: an executor drain, a capture-store teardown and a chunk decode were
each a confident explanation and each disproved by measurement, while the real
one (coverage and lazy mount fighting over the same message pump) was found by a
test rather than by any of them.

So this records rather than infers. A heartbeat wants to wake every 50ms; when it
wakes late by more than the threshold, it writes the delay and a snapshot of what
the preview was doing at that moment. Off unless ``_FND_STALL_WATCH`` is set,
which may carry a millisecond threshold (``_FND_STALL_WATCH=250``).

The cost when enabled is one timer wake-up per 50ms and no work at all unless a
stall actually happens, so it is safe to leave on for a session of real use.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fnd.tui.app import FNDApp

__all__ = ["StallWatch"]

_TICK = 0.05
_DEFAULT_THRESHOLD_MS = 400.0


class StallWatch:
    """Logs event-loop stalls, with the preview's state at the time."""

    def __init__(self, app: FNDApp, threshold_ms: float = _DEFAULT_THRESHOLD_MS) -> None:
        self._app = app
        self._threshold_ms = threshold_ms
        self._task: asyncio.Task[None] | None = None

    @classmethod
    def from_env(cls, app: FNDApp) -> StallWatch | None:
        """A watch if ``_FND_STALL_WATCH`` asks for one, else ``None``."""
        import os

        raw = os.environ.get("_FND_STALL_WATCH")
        if not raw or raw == "0":
            return None
        try:
            threshold = float(raw)
        except ValueError:
            threshold = _DEFAULT_THRESHOLD_MS
        # `=1` means "on", not "stall on everything over a millisecond".
        if threshold <= 1:
            threshold = _DEFAULT_THRESHOLD_MS
        return cls(app, threshold)

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()

    async def _run(self) -> None:
        app = self._app
        app._diag_log(f"stall watch armed threshold={self._threshold_ms:.0f}ms")
        last = time.perf_counter()
        while True:
            await asyncio.sleep(_TICK)
            now = time.perf_counter()
            late = (now - last - _TICK) * 1000
            last = now
            if late >= self._threshold_ms:
                app._diag_log(f"STALL {late:.0f}ms  {self._snapshot()}")

    def _snapshot(self) -> str:
        """What the preview was doing. Every read is guarded: a snapshot that
        raises during a stall would replace the evidence with a traceback."""
        bits: list[str] = []
        preview = getattr(self._app, "_preview", None)
        try:
            bits.append(f"capturing={getattr(preview, 'coverage_activity', None)}")
        except Exception:  # pragma: no cover - defensive
            bits.append("capturing=?")
        for label, probe in (
            ("mount", lambda: preview is not None and preview.user_mount_in_flight()),
            ("settling", lambda: self._app._preview_scroll.is_settling),
            ("lazy", lambda: not self._lazy_idle()),
            ("busy", lambda: preview is not None and preview.pipeline_busy()),
        ):
            try:
                bits.append(f"{label}={probe()}")
            except Exception:  # pragma: no cover - defensive
                bits.append(f"{label}=?")
        return " ".join(bits)

    def _lazy_idle(self) -> bool:
        task = getattr(getattr(self._app, "_lazy", None), "task", None)
        return task is None or bool(task.done())  # type: ignore[attr-defined]

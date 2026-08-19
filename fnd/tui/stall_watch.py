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
import math
import os
import sys
import tempfile
import threading
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fnd.tui.app import FNDApp

__all__ = ["StallWatch"]

_TICK = 0.05
_DEFAULT_THRESHOLD_MS = 400.0
# How late the loop must be before the sampler bothers taking a stack. Well
# below the report threshold: the point is to catch the stall while it is still
# happening, not to agree with the reporter after the fact.
_SAMPLE_AFTER = 0.2
_SAMPLE_FILE = "fnd-stall-stacks.log"


class StallWatch:
    """Logs event-loop stalls, with the preview's state at the time."""

    def __init__(self, app: FNDApp, threshold_ms: float = _DEFAULT_THRESHOLD_MS) -> None:
        self._app = app
        self._threshold_ms = threshold_ms
        self._task: asyncio.Task[None] | None = None
        # Sampler scaffolding: the watch itself cannot see a stall from inside
        # the loop it is blocked on, so a thread samples the main thread while
        # the loop is away.
        self._beat = time.perf_counter()
        self._stop_sampler = threading.Event()
        self._stacks: Counter[str] = Counter()
        self._sample_path = Path(tempfile.gettempdir()) / _SAMPLE_FILE

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
        # `isfinite` because `inf`/`nan` pass the bound below and then silently
        # disable every report — a diagnostic that says nothing looks like a
        # clean run.
        if not math.isfinite(threshold) or threshold <= 1:
            threshold = _DEFAULT_THRESHOLD_MS
        return cls(app, threshold)

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())
        if os.environ.get("_FND_STALL_STACKS"):
            self._sample_path = Path(tempfile.gettempdir()) / _SAMPLE_FILE
            threading.Thread(target=self._sample, daemon=True).start()

    def _sample(self) -> None:
        """Sample the main thread's stack whenever the loop is late.

        A thread, not a task, and that is the whole point: a coroutine cannot
        observe the loop it is itself blocked on, so the flags in
        :meth:`_snapshot` can only ever say which of OUR markers was set — never
        which code was actually running. Sampling from outside answers that
        directly, and it is what finally attributed these stalls (108 of 110
        samples inside ``Stylesheet.apply``, over half of them the descendant
        restyle now shortcut by ``preview/visibility.py``) after several
        confident flag-based guesses had each been disproved.

        Written out line by line rather than summarised at exit, because a
        session that is killed rather than quit never reaches the summary.
        """
        main_id = threading.main_thread().ident
        while not self._stop_sampler.wait(0.02):
            late = time.perf_counter() - self._beat
            if late < _SAMPLE_AFTER or main_id is None:
                continue
            frame = sys._current_frames().get(main_id)
            if frame is None:
                continue
            stack = traceback.extract_stack(frame)
            trimmed = [f for f in stack if "/fnd/" in f.filename or "/textual/" in f.filename]
            key = " < ".join(
                f"{f.filename.rsplit('/', 1)[-1]}:{f.name}" for f in reversed(trimmed[-14:])
            )
            self._stacks[key] += 1
            try:
                with self._sample_path.open("a") as fh:
                    fh.write(f"{late * 1000:.0f} {key}\n")
            except Exception:  # pragma: no cover - diagnostics never break a run
                pass

    def dump_stacks(self) -> None:
        for key, count in self._stacks.most_common(25):
            self._app._diag_log(f"SAMPLE {count:4d}  {key}")

    def stop(self) -> None:
        self._stop_sampler.set()
        self.dump_stacks()
        task = self._task
        if task is not None and not task.done():
            task.cancel()

    async def _run(self) -> None:
        app = self._app
        app._diag_log(f"stall watch armed threshold={self._threshold_ms:.0f}ms")
        last = time.perf_counter()
        self._beat = last
        last_cpu = time.process_time()
        # The state as it was just before we went to sleep. THIS is the one that
        # names a stall: by the time we wake, the work that held the loop has
        # finished and cleared its own marker, so sampling only on waking
        # reports the successor and exonerates the culprit every time.
        before = self._snapshot()
        while True:
            await asyncio.sleep(_TICK)
            now = time.perf_counter()
            self._beat = now
            cpu = time.process_time()
            late = (now - last - _TICK) * 1000
            burned = (cpu - last_cpu) * 1000
            last, last_cpu = now, cpu
            if late >= self._threshold_ms:
                # CPU consumed across the gap, reported rather than judged.
                # A heartbeat alone cannot tell a blocked loop from a process
                # the OS stopped running — an unfocused terminal gets its timers
                # coalesced, and a 7.7s gap nobody felt reads exactly like a
                # 7.7s freeze. CPU close to the gap means Python work held the
                # loop; CPU near zero means the process was either idle or
                # waiting on something outside it. Deliberately NOT a verdict:
                # a blocking read would also burn no CPU while genuinely
                # freezing the UI, and mislabelling that would send the next
                # investigation the wrong way.
                app._diag_log(
                    f"STALL {late:.0f}ms cpu={burned:.0f}ms  "
                    f"before[{before}]  after[{self._snapshot()}]"
                )
            before = self._snapshot()

    def _snapshot(self) -> str:
        """What the preview was doing. Every read is guarded: a snapshot that
        raises during a stall would replace the evidence with a traceback."""
        bits: list[str] = []
        preview = getattr(self._app, "_preview", None)
        try:
            bits.append(f"capturing={getattr(preview, 'coverage_activity', None)}")
            bits.append(f"phase={getattr(preview, 'mount_phase', None)}")
        except Exception:  # pragma: no cover - defensive
            bits.append("capturing=?")
        for label, probe in (
            ("mount", lambda: preview is not None and preview.user_mount_in_flight()),
            ("settling", lambda: self._app._preview_scroll.is_settling),
            ("lazy", lambda: not self._lazy_idle()),
            ("busy", lambda: preview is not None and preview.pipeline_busy()),
            ("prefetch", self._prefetch_state),
        ):
            try:
                bits.append(f"{label}={probe()}")
            except Exception:  # pragma: no cover - defensive
                bits.append(f"{label}=?")
        return " ".join(bits)

    def _prefetch_state(self) -> str:
        """What prefetch is doing: the running job's file, and the queue depth.

        Prefetch mounts widgets on the loop, so it can hold it — and it is the
        one background actor no other flag here covers, which is why stalls kept
        being reported with everything False.
        """
        prefetch = getattr(self._app, "_prefetch", None)
        if prefetch is None:
            return "none"
        queue = getattr(prefetch, "sink_queue", None)
        depth = queue.qsize() if queue is not None else 0
        return f"{getattr(prefetch, 'active_job', None)}/q{depth}"

    def _lazy_idle(self) -> bool:
        task = getattr(getattr(self._app, "_lazy", None), "task", None)
        return task is None or bool(task.done())  # type: ignore[attr-defined]

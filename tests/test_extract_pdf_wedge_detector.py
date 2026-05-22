"""Heartbeat-driven wedge detection for the PDF extractor subprocess.

A genuinely hung native call (rare but possible) inside the subprocess
worker would leave the indexer stuck forever during an unattended run,
because the asyncio loop has no signal that progress has stopped. The
worker emits a heartbeat into a multiprocessing.Queue on each per-page
boundary; the parent watches that queue and kills the worker if no
heartbeat arrives for ``stall_seconds``.

The threshold is intentionally large (120s by default) so a slow but
genuinely-progressing PDF stays alive. The signal we are reading is
"no forward progress", not "exceeded a budget".
"""

from __future__ import annotations

import time
from typing import Any

import pytest


def _quiet_worker(queue: Any, seconds: float) -> str:
    """Sleeps without putting anything on the queue. Used to simulate
    a wedged native call inside the subprocess."""
    time.sleep(seconds)
    return "done"


def _chatty_worker(queue: Any, n_beats: int, interval: float) -> str:
    """Emits ``n_beats`` heartbeats ``interval`` seconds apart, then
    returns. Models a slow-but-progressing extractor."""
    for i in range(n_beats):
        queue.put(("beat", i))
        time.sleep(interval)
    return "done"


# spawn + Python interpreter init + module imports on macOS run ~3-5 s on
# a cold pool. Tests need a first-beat grace window above that so we
# only measure the steady-state stall behaviour.
_GRACE = 15.0


def test_stall_detector_kills_quiet_worker() -> None:
    """A worker that does not heartbeat within ``stall_seconds`` is
    killed and the parent surfaces a StallError. The worker would
    otherwise tie up the indexer forever."""
    from fnd.extract._worker import (
        StallError,
        run_in_pool_sync_with_stall_detection,
    )

    t0 = time.monotonic()
    with pytest.raises(StallError):
        run_in_pool_sync_with_stall_detection(
            _quiet_worker,
            30.0,
            stall_seconds=0.5,
            first_beat_grace_seconds=_GRACE,
        )
    # Must trigger well before the worker's full sleep.
    assert time.monotonic() - t0 < _GRACE + 5.0


def test_chatty_worker_survives_stall_detection() -> None:
    """A worker that puts heartbeats on the queue faster than
    ``stall_seconds`` completes normally. The stall detector must
    not fire when forward progress is visible."""
    from fnd.extract._worker import run_in_pool_sync_with_stall_detection

    result = run_in_pool_sync_with_stall_detection(
        _chatty_worker,
        8,
        0.1,
        stall_seconds=0.5,
        first_beat_grace_seconds=_GRACE,
    )
    assert result == "done"


def test_pool_recovers_after_stall_kill() -> None:
    """After the stall detector kills a worker, the next call must
    succeed against a fresh worker. One wedged PDF cannot poison the
    rest of an indexing run."""
    from fnd.extract._worker import (
        StallError,
        run_in_pool_sync_with_stall_detection,
    )

    with pytest.raises(StallError):
        run_in_pool_sync_with_stall_detection(
            _quiet_worker,
            30.0,
            stall_seconds=0.5,
            first_beat_grace_seconds=_GRACE,
        )

    # Pool must come back; second call completes normally.
    result = run_in_pool_sync_with_stall_detection(
        _chatty_worker,
        3,
        0.05,
        stall_seconds=0.5,
        first_beat_grace_seconds=_GRACE,
    )
    assert result == "done"

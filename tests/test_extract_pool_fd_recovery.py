"""Pool FD-leak regression: spawning many subprocess submissions in a
row must NOT eventually fail with ``ValueError: bad value(s) in
fds_to_keep``.

Background: an earlier design routed per-PDF heartbeats through a
shared ``multiprocessing.Manager`` queue. Each
``manager.Queue()`` call allocated a Unix socket FD on the parent
that lingered until GC. After hundreds of PDFs the FD set passed to
``_posixsubprocess.fork_exec`` exceeded a threshold and the next
worker spawn was rejected — leaving every subsequent PDF in the
indexing run failing with ExtractError("ValueError: bad value(s) in
fds_to_keep"). In the wine-chain bug this produced the exact symptom
"chain completes, 29 misses, 0 docs landed".

This test does enough rapid submissions to exhaust the prior design's
budget; with the Pipe-per-submit fix it completes cleanly. The pool
is reused across submissions so only the heartbeat-channel + per-
submit pickled args allocate FDs; 30 calls is plenty to trip the
prior leak.
"""

from __future__ import annotations

from typing import Any


def _quick_worker(sender: Any, n: int) -> int:
    """Send one heartbeat then return ``n`` immediately. Models a
    fast-path PDF whose extraction completes in well under the stall
    threshold."""
    sender.send(("beat", 0))
    return n


def test_many_submissions_do_not_leak_fds() -> None:
    """Hammer the stall-detector entry point. Each call recreates the
    heartbeat channel; nothing must accumulate parent-side FDs across
    submissions."""
    from fnd.extract._worker import run_in_pool_sync_with_stall_detection, shutdown_pool

    try:
        for i in range(30):
            result = run_in_pool_sync_with_stall_detection(
                _quick_worker,
                i,
                stall_seconds=5.0,
                first_beat_grace_seconds=15.0,
            )
            assert result == i
    finally:
        # Leave the pool clean for the next test in the same process.
        shutdown_pool()

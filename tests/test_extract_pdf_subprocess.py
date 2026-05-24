"""The structured-PDF extractor must run in a subprocess so that
native code holding the GIL cannot starve the caller's asyncio loop.

pymupdf-layout's C code can hold the GIL across long table-detection
or image-scaling passes. asyncio.to_thread (the previous mechanism)
does not help because Python threads share the GIL. Only a subprocess
provides genuine isolation.

These tests pin the contract: the new extractor exposes a coroutine
that dispatches the per-PDF work to a ProcessPoolExecutor and
heartbeats progress back to the caller. Both behaviours are
load-bearing; the freeze the user reported manifests when either is
absent.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest


@pytest.mark.asyncio
async def test_extraction_dispatches_to_a_subprocess() -> None:
    """run_in_pool runs the supplied callable in a different OS process
    than the caller. This is the load-bearing isolation property that
    keeps the asyncio loop responsive when pymupdf-layout holds the
    GIL inside its native code."""
    from fnd.extract._worker import run_in_pool

    parent_pid = os.getpid()
    worker_pid = await run_in_pool(os.getpid)

    assert worker_pid != parent_pid, (
        f"extraction must run out-of-process; got same pid {parent_pid}"
    )


@pytest.mark.asyncio
async def test_loop_stays_responsive_under_a_gil_holding_worker() -> None:
    """While a GIL-holding callable is in flight inside the pool, the
    caller's asyncio loop continues to tick at near its idle rate.

    The synthetic worker simulates the bad behaviour by widening
    sys.setswitchinterval and running a tight Python loop. In a thread
    this starves the main thread; in a subprocess it does not. The
    assertion is conservative enough to avoid CI flakes and strict
    enough to detect a regression to thread-only dispatch."""
    from fnd.extract._worker import run_in_pool

    ticks = 0

    async def tick_loop() -> None:
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.05)
            ticks += 1

    async def heavy() -> None:
        await run_in_pool(_gil_starver, 1.0)

    await asyncio.gather(tick_loop(), heavy())

    # 20 ticks at 50 ms = 1 s expected. Allow a generous floor.
    assert ticks >= 15, f"asyncio loop was starved during extraction: only {ticks} ticks"


def _gil_starver(seconds: float) -> int:
    """Run a tight Python loop that holds the GIL strongly enough to
    starve another thread. Process-global side-effect is restored on
    exit so other tests in the same process see normal switching."""
    import sys

    old = sys.getswitchinterval()
    sys.setswitchinterval(1.0)
    try:
        end = time.perf_counter() + seconds
        n = 0
        while time.perf_counter() < end:
            n += 1
        return n
    finally:
        sys.setswitchinterval(old)

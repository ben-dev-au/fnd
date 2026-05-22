"""Subprocess dispatch for structured-PDF extraction.

pymupdf-layout's C code can hold the GIL across long extractions,
starving the caller's asyncio loop when run via ``asyncio.to_thread``.
Dispatching through this module's ProcessPoolExecutor runs the work
in a separate OS process, so GIL holding is invisible to the caller.

Usage:

    chunks = await run_in_pool(_extract_inner, path)

The submitted callable and its arguments must be picklable.
"""

from __future__ import annotations

import asyncio
import atexit
import multiprocessing as mp
import os
import queue as queue_mod
import signal
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from typing import Any


class StallError(RuntimeError):
    """Raised when the subprocess worker stopped emitting heartbeats
    while still holding a task. The worker has been killed; the next
    call gets a fresh worker."""


# Module-level singletons. Lazy-initialised so importing the module is
# cheap (no fork / no manager server) and idempotent: subsequent
# callers after shutdown get fresh instances.
_pool: ProcessPoolExecutor | None = None
_manager: Any = None  # multiprocessing.managers.SyncManager


def _get_pool() -> ProcessPoolExecutor:
    global _pool
    if _pool is None:
        # ``spawn`` is the macOS-safe start method; ``fork`` deadlocks
        # under native libs that pre-spawn threads (pymupdf does).
        _pool = ProcessPoolExecutor(
            max_workers=1,
            mp_context=mp.get_context("spawn"),
        )
        atexit.register(shutdown_pool)
    return _pool


def shutdown_pool() -> None:
    """Tear down the pool. Safe to call multiple times."""
    global _pool
    if _pool is not None:
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None


def _get_manager() -> Any:
    global _manager
    if _manager is None:
        _manager = mp.get_context("spawn").Manager()
        atexit.register(_shutdown_manager)
    return _manager


def _shutdown_manager() -> None:
    global _manager
    if _manager is not None:
        with _suppress(Exception):
            _manager.shutdown()
        _manager = None


def _kill_pool_workers(pool: ProcessPoolExecutor) -> None:
    """SIGKILL every running worker. The pool raises BrokenProcessPool
    on the in-flight future when it notices the death."""
    # ProcessPoolExecutor does not expose its workers via public API.
    # The internal dict is stable across Python 3.7+ and the cost of
    # a quirky look-up is much smaller than the cost of writing our
    # own pool. ``getattr`` with default keeps the call site safe if
    # the attribute name ever changes upstream.
    procs = getattr(pool, "_processes", {}) or {}
    for proc in list(procs.values()):
        with _suppress(ProcessLookupError, PermissionError, AttributeError):
            os.kill(proc.pid, signal.SIGKILL)


def _suppress(*excs: type[BaseException]) -> Any:
    import contextlib

    return contextlib.suppress(*excs)


def collect_pdf_chunks(path: Any, skip_structure: bool) -> list[Any]:
    """Subprocess entrypoint for PDF extraction.

    Lives in this module (not ``fnd.extract.pdf``) so that pickle's
    identity check survives the ``del sys.modules['fnd.extract.pdf']``
    pattern used by some tests: this module is never deleted, so
    ``collect_pdf_chunks.__module__`` always resolves to the same
    function object.

    The subprocess re-imports ``fnd.extract.pdf`` fresh (its module
    globals start at defaults). This helper re-establishes the
    run-scoped flag the parent had set, then drains
    ``_extract_inner`` into a list (subprocess boundaries cannot
    ferry generators)."""
    from fnd.extract import pdf as _pdf

    _pdf.set_skip_structure_extraction(skip_structure)
    return list(_pdf._extract_inner(path))


def collect_pdf_chunks_with_heartbeat(queue: Any, path: Any, skip_structure: bool) -> list[Any]:
    """Like :func:`collect_pdf_chunks` but emits a per-page heartbeat
    to ``queue`` so the parent's stall detector can tell a slow PDF
    apart from a wedged one."""
    from fnd.extract import pdf as _pdf

    _pdf.set_skip_structure_extraction(skip_structure)

    def _on_page(page_index: int) -> None:
        # ``put_nowait`` so a wedged parent (queue full) does not block
        # the extractor. The parent always drains as fast as it can;
        # falling behind is fine, dropping a beat does not hurt
        # correctness (the next beat resets the watchdog).
        with _suppress(queue_mod.Full, BrokenPipeError, EOFError):
            queue.put_nowait(("page", page_index))

    return list(_pdf._extract_inner(path, on_page=_on_page))


async def run_in_pool[T](fn: Callable[..., T], *args: Any) -> T:
    """Dispatch ``fn(*args)`` to the extraction pool and await its
    result. The callable runs in a separate OS process; the caller's
    asyncio loop stays responsive throughout.

    ``fn`` and its arguments must be picklable.
    """
    pool = _get_pool()
    future = pool.submit(fn, *args)
    return await asyncio.wrap_future(future)


def run_in_pool_sync[T](fn: Callable[..., T], *args: Any) -> T:
    """Synchronous variant of :func:`run_in_pool`. Use when the caller
    is already on a worker thread (the asyncio.to_thread body inside
    ``run_indexer``); the thread blocks on the subprocess result via
    ``Future.result()`` which releases the GIL, leaving the main
    thread's asyncio loop free."""
    from concurrent.futures.process import BrokenProcessPool

    pool = _get_pool()
    try:
        return pool.submit(fn, *args).result()
    except BrokenProcessPool:
        # Native crash in the worker (segfault, OOM-killed). Tear the
        # pool down so the NEXT PDF gets a healthy worker instead of
        # cascading failures for the rest of the indexing run.
        shutdown_pool()
        raise


def run_in_pool_sync_with_stall_detection[T](
    fn: Callable[..., T],
    *args: Any,
    stall_seconds: float = 120.0,
    first_beat_grace_seconds: float | None = None,
) -> T:
    """Submit ``fn(queue, *args)`` to the pool. The worker must put
    items on ``queue`` while making forward progress; if no item
    arrives within ``stall_seconds`` the worker is killed and
    :class:`StallError` is raised. The next call gets a fresh worker.

    ``first_beat_grace_seconds`` is the budget the worker has to
    produce its FIRST heartbeat (covering subprocess spawn + module
    import + first-page extraction). Once a heartbeat arrives, the
    enforcement switches to ``stall_seconds`` between beats. Defaults
    to ``stall_seconds`` when unset.

    The threshold is intentionally large so a legitimately slow but
    progressing PDF (image-dense pages, docling fallback, large file)
    stays alive. We are detecting "no progress at all", not "took
    longer than a budget"."""
    from concurrent.futures.process import BrokenProcessPool

    grace = first_beat_grace_seconds if first_beat_grace_seconds is not None else stall_seconds
    queue = _get_manager().Queue()
    pool = _get_pool()
    future = pool.submit(fn, queue, *args)

    start = time.monotonic()
    last_beat: float | None = None
    # Keep the poll shorter than stall_seconds so a long get() does not
    # falsely trip the stall check after a fast-completing worker.
    poll_seconds = max(0.05, min(1.0, stall_seconds / 4.0))
    while not future.done():
        try:
            queue.get(timeout=poll_seconds)
            last_beat = time.monotonic()
        except queue_mod.Empty:
            pass

        # If the worker returned while we were blocked in queue.get, do
        # not raise a stall against a future that has already produced
        # its result.
        if future.done():
            break

        now = time.monotonic()
        if last_beat is not None:
            baseline, threshold = last_beat, stall_seconds
        else:
            baseline, threshold = start, grace
        if now - baseline > threshold:
            _kill_pool_workers(pool)
            shutdown_pool()
            raise StallError(f"no heartbeat for {threshold:.1f}s; worker killed")

    try:
        return future.result()
    except BrokenProcessPool:
        shutdown_pool()
        raise

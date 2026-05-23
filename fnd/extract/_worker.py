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
import signal
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from typing import Any


class StallError(RuntimeError):
    """Raised when the subprocess worker stopped emitting heartbeats
    while still holding a task. The worker has been killed; the next
    call gets a fresh worker."""


# Module-level pool singleton. Lazy-initialised so importing the module
# is cheap (no fork) and idempotent: subsequent callers after shutdown
# get a fresh executor.
_pool: ProcessPoolExecutor | None = None


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
    """Tear down the pool. Safe to call multiple times.

    Waits for in-flight work so the executor's internal pipes are
    released before we drop the reference, then forces a GC pass so
    finalizers actually close the pipe FDs. The previous ``wait=False``
    plus no GC raced the next ``_get_pool()`` and left stale FDs
    visible to ``_posixsubprocess.fork_exec`` in the new worker spawn
    ("bad value(s) in fds_to_keep") — the exact symptom of the
    wine-chain bug.
    """
    import gc

    global _pool
    if _pool is not None:
        _pool.shutdown(wait=True, cancel_futures=True)
        _pool = None
    # ProcessPoolExecutor holds pipe FDs inside _processes / management
    # thread state; the pipe finalizers release them only when those
    # objects hit zero refcount. Force a GC pass before any next spawn.
    gc.collect()


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


def collect_pdf_chunks_with_heartbeat(
    heartbeat_sender: Any, path: Any, skip_structure: bool
) -> list[Any]:
    """Like :func:`collect_pdf_chunks` but emits a per-page heartbeat
    on ``heartbeat_sender`` (a :class:`multiprocessing.Connection`) so
    the parent's stall detector can tell a slow PDF apart from a
    wedged one."""
    from fnd.extract import pdf as _pdf

    _pdf.set_skip_structure_extraction(skip_structure)

    def _on_page(page_index: int) -> None:
        # ``send`` is non-blocking against a small pipe buffer for the
        # tiny payloads we use; the parent drains as fast as it can.
        # Dropping a beat does not hurt correctness — the next beat
        # resets the watchdog.
        with _suppress(BrokenPipeError, EOFError, OSError):
            heartbeat_sender.send(("page", page_index))

    try:
        return list(_pdf._extract_inner(path, on_page=_on_page))
    finally:
        with _suppress(Exception):
            heartbeat_sender.close()


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

    last_exc: BaseException | None = None
    for attempt in range(2):
        pool = _get_pool()
        try:
            return pool.submit(fn, *args).result()
        except BrokenProcessPool as e:
            # Native crash in the worker (segfault, OOM-killed). Tear
            # the pool down so the NEXT PDF gets a healthy worker
            # instead of cascading failures for the rest of the run.
            last_exc = e
            shutdown_pool()
            if attempt == 0:
                continue
            raise
        except Exception as e:
            last_exc = e
            if _is_stale_fd_error(e) and attempt == 0:
                shutdown_pool()
                continue
            raise
    assert last_exc is not None
    raise last_exc


def _is_stale_fd_error(exc: BaseException) -> bool:
    """Detect ``ValueError: bad value(s) in fds_to_keep`` raised by
    ``_posixsubprocess.fork_exec`` when the parent's FD table contains
    a closed FD that the spawn-method tried to inherit into the new
    worker. Recovery is a full pool teardown; the message text is the
    only public signal of this state."""
    return isinstance(exc, ValueError) and "fds_to_keep" in str(exc)


def run_in_pool_sync_with_stall_detection[T](
    fn: Callable[..., T],
    *args: Any,
    stall_seconds: float = 120.0,
    first_beat_grace_seconds: float | None = None,
) -> T:
    """Submit ``fn(sender, *args)`` to the pool. The worker must
    ``send`` heartbeats on the supplied
    :class:`multiprocessing.Connection` while making forward progress;
    if no heartbeat arrives within ``stall_seconds`` the worker is
    killed and :class:`StallError` is raised. The next call gets a
    fresh worker.

    ``first_beat_grace_seconds`` is the budget the worker has to
    produce its FIRST heartbeat (covering subprocess spawn + module
    import + first-page extraction). Once a heartbeat arrives, the
    enforcement switches to ``stall_seconds`` between beats. Defaults
    to ``stall_seconds`` when unset.

    The threshold is intentionally large so a legitimately slow but
    progressing PDF (image-dense pages, docling fallback, large file)
    stays alive. We are detecting "no progress at all", not "took
    longer than a budget"."""
    # ``fds_to_keep`` recovery retries once with a fresh pool; if the
    # second attempt also fails the caller surfaces the error.
    last_exc: BaseException | None = None
    for attempt in range(2):
        try:
            return _submit_with_stall_detection(
                fn,
                *args,
                stall_seconds=stall_seconds,
                first_beat_grace_seconds=first_beat_grace_seconds,
            )
        except Exception as e:
            last_exc = e
            if _is_stale_fd_error(e) and attempt == 0:
                # Tear down pool + manager so the next spawn starts
                # with a clean FD set. Without this, every subsequent
                # PDF in the run fails the same way (the original
                # symptom in the wine-chain bug).
                shutdown_pool()
                continue
            raise
    assert last_exc is not None
    raise last_exc


def _submit_with_stall_detection[T](
    fn: Callable[..., T],
    *args: Any,
    stall_seconds: float,
    first_beat_grace_seconds: float | None,
) -> T:
    from concurrent.futures.process import BrokenProcessPool

    grace = first_beat_grace_seconds if first_beat_grace_seconds is not None else stall_seconds
    # Per-submit Pipe instead of a long-lived Manager Queue. Manager
    # queues went through a separate server process; each
    # ``manager.Queue()`` call allocated a Unix socket FD on the parent
    # that lingered until GC, eventually overflowing the FD set passed
    # to ``_posixsubprocess.fork_exec`` ("bad value(s) in fds_to_keep")
    # and breaking every subsequent worker spawn. A raw Pipe owns just
    # two FDs that we close explicitly after the worker is done with
    # them (closing earlier would race the spawn and synthesise the
    # very fds_to_keep failure we are trying to avoid).
    ctx = mp.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    pool = _get_pool()
    future = pool.submit(fn, child_conn, *args)

    start = time.monotonic()
    last_beat: float | None = None
    # Keep the poll shorter than stall_seconds so a long poll does not
    # falsely trip the stall check after a fast-completing worker.
    poll_seconds = max(0.05, min(1.0, stall_seconds / 4.0))
    try:
        while not future.done():
            try:
                if parent_conn.poll(poll_seconds):
                    parent_conn.recv()
                    last_beat = time.monotonic()
            except (EOFError, OSError):
                # Worker closed its end; rely on future.done() / result
                # to surface the real outcome.
                break

            # If the worker returned while we were blocked in poll, do
            # not raise a stall against a future that has already
            # produced its result.
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
    finally:
        # Worker has finished (or been killed); now safe to release
        # both ends. Without this, each submission would leave a Pipe's
        # two FDs lingering in the parent until GC.
        with _suppress(Exception):
            parent_conn.close()
        with _suppress(Exception):
            child_conn.close()

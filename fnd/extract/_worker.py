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
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from typing import Any

# Module-level singleton. Lazy-initialised so importing the module is
# cheap (no fork) and idempotent: a subsequent caller after shutdown
# gets a fresh pool.
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
    """Tear down the pool. Safe to call multiple times."""
    global _pool
    if _pool is not None:
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None


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

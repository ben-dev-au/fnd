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
import faulthandler
import multiprocessing as mp
import os
import signal
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from typing import Any

# Optional per-process trace. FND_WORKER_TRACE=/path enables it; unset
# means zero cost. Lets a future cascade debug tell "worker never
# spawned" from "worker spawned but died inside native code" - both
# look identical from the parent's BrokenProcessPool.
_trace_path = os.environ.get("FND_WORKER_TRACE")


def _trace(msg: str) -> None:
    if not _trace_path:
        return
    try:
        with open(_trace_path, "a") as f:
            f.write(f"[{os.getpid()}] {msg}\n")
    except OSError:
        pass


if _trace_path:
    _trace(f"module-imported ppid={os.getppid()}")
    try:
        # faulthandler needs the file kept open for the process lifetime,
        # so a `with` block here would close it too early.
        _fh_file = open(_trace_path, "a")  # noqa: SIM115
        faulthandler.enable(file=_fh_file, all_threads=True)
    except OSError:
        pass


class StallError(RuntimeError):
    """Raised when the subprocess worker stopped emitting heartbeats
    while still holding a task. The worker has been killed; the next
    call gets a fresh worker."""


# Module-level pool singleton. Lazy-initialised so importing the module
# is cheap (no fork) and idempotent: subsequent callers after shutdown
# get a fresh executor.
_pool: ProcessPoolExecutor | None = None

# Cancel beacon. When the TUI's action_cancel sets this, the retry
# wrapper sees it and bails after the first attempt instead of
# spawning a fresh worker on the same slow PDF (which would restart
# the user's wait clock). Cleared at the start of every run.
_cancel_requested = False


def request_cancel() -> None:
    global _cancel_requested
    _cancel_requested = True


def clear_cancel() -> None:
    global _cancel_requested
    _cancel_requested = False


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


def _noop_worker() -> int:
    return 0


def warm_pool() -> None:
    """Spawn the PDF extraction worker now, before the caller does
    anything that would mutate the parent's FD table.

    Once Textual's run() has rewired stdin/stderr and registered
    signal-wakeup pipes, ``_posixsubprocess.fork_exec`` rejects the
    next spawn with ``ValueError: bad value(s) in fds_to_keep`` and
    every PDF in the chain fails. Forcing the spawn now (the submit-
    and-wait actually starts the subprocess, not just the executor's
    bookkeeping) captures the clean FD state."""
    import contextlib

    pool = _get_pool()
    # Best-effort: if warming fails, the regular extraction path will
    # surface the real error. Don't crash the TUI launch.
    with contextlib.suppress(Exception):
        pool.submit(_noop_worker).result(timeout=30)


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


def _redirect_native_stderr_to_log() -> None:
    """Point FD 2 of the worker at a log file under the user cache.

    pymupdf-layout calls leptonica, which prints "pixScaleSmooth" /
    "Image too small to scale" via C-level fprintf(stderr). Python's
    sys.stderr redirect is invisible to that, so the warnings would
    otherwise flood the parent tmux pane on top of the IndexerScreen
    modal during every PDF-heavy reindex."""
    try:
        from platformdirs import user_cache_dir

        log_dir = os.path.join(user_cache_dir("fnd"), "worker-logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "extractor-stderr.log")
        log_fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        os.dup2(log_fd, 2)
        os.close(log_fd)
    except OSError:
        pass


def _maybe_inject_test_slowdown(heartbeat_sender: Any) -> None:
    """Test hook: when FND_TEST_SLOW_EXTRACT_SECONDS is set, sleep
    that many seconds before extracting so the UI can observably
    test cancel + stall behaviour. Sends one heartbeat first so the
    stall detector doesn't time out during the sleep."""
    raw = os.environ.get("FND_TEST_SLOW_EXTRACT_SECONDS")
    if not raw:
        return
    try:
        secs = float(raw)
    except ValueError:
        return
    with _suppress(BrokenPipeError, EOFError, OSError):
        heartbeat_sender.send(("test-slowdown-start", 0))
    chunks = max(1, int(secs))
    for i in range(chunks):
        time.sleep(secs / chunks)
        with _suppress(BrokenPipeError, EOFError, OSError):
            heartbeat_sender.send(("test-slowdown-tick", i))


def collect_pdf_chunks_with_heartbeat(
    heartbeat_sender: Any, path: Any, skip_structure: bool
) -> list[Any]:
    """Like :func:`collect_pdf_chunks` but emits a per-page heartbeat
    on ``heartbeat_sender`` (a :class:`multiprocessing.Connection`) so
    the parent's stall detector can tell a slow PDF apart from a
    wedged one. Also emits a ("total", N) beat at the start so the
    parent's ETA can size the per-page rate against the file's actual
    length, and a ("file-start", path) beat so the parent can reset
    per-file timing state on a new extraction."""
    _redirect_native_stderr_to_log()
    _maybe_inject_test_slowdown(heartbeat_sender)
    _trace(f"enter path={path}")
    # Probe the page count cheaply so the parent can refine ETA
    # mid-extraction. pymupdf opens the doc twice (once here, once
    # in _extract_inner) but the open is sub-ms and lets the user's
    # ETA reflect "I'm 12/247 pages into a long PDF" instead of
    # ticking blindly until the file completes.
    # Probe is best-effort - extraction itself owns the real error
    # surface; if it can't open, we just skip the heartbeat hint.
    with _suppress(Exception):
        import pymupdf as _pymupdf

        _doc = _pymupdf.open(str(path))
        with _suppress(BrokenPipeError, EOFError, OSError):
            heartbeat_sender.send(("file-start", str(path)))
            heartbeat_sender.send(("total", _doc.page_count))
        _doc.close()

    from fnd.extract import pdf as _pdf

    _pdf.set_skip_structure_extraction(skip_structure)

    def _on_page(page_index: int) -> None:
        with _suppress(BrokenPipeError, EOFError, OSError):
            heartbeat_sender.send(("page", page_index))

    try:
        chunks = list(_pdf._extract_inner(path, on_page=_on_page))
        _trace(f"exit chunks={len(chunks)} path={path}")
        return chunks
    except BaseException as e:
        _trace(f"raise {type(e).__name__}: {e}")
        raise
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
    on_heartbeat: Callable[[Any], None] | None = None,
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
    from concurrent.futures.process import BrokenProcessPool

    # ``fds_to_keep`` recovery retries once with a fresh pool; if the
    # second attempt also fails the caller surfaces the error.
    # BrokenProcessPool from a transient worker death (inherited bad
    # state after a sibling PDF crashed pymupdf, OOM-killer hit a
    # neighbour) also gets one retry: the inner handler has already
    # torn the pool down, so the retry spawns a fresh worker. A
    # truly-poisonous PDF still fails on the second attempt and
    # surfaces as ExtractError.
    last_exc: BaseException | None = None
    for attempt in range(2):
        try:
            return _submit_with_stall_detection(
                fn,
                *args,
                stall_seconds=stall_seconds,
                first_beat_grace_seconds=first_beat_grace_seconds,
                on_heartbeat=on_heartbeat,
            )
        except BrokenProcessPool as e:
            last_exc = e
            # Skip the retry when the user has cancelled - the kill
            # they just issued is exactly what triggered this
            # BrokenProcessPool, and respawning would put the same
            # slow PDF back on the worker (Cancel would never
            # actually cancel).
            if attempt == 0 and not _cancel_requested:
                # Pool was already shutdown_pool'd by the inner handler.
                continue
            raise
        except StallError as e:
            # A single stalled PDF should not surface as ExtractError
            # for the whole file; the stall handler already killed the
            # worker + tore down the pool, so the retry spawns a
            # fresh worker. A truly poisonous PDF stalls both attempts
            # and surfaces honestly. Cancel bypasses the retry for
            # the same reason as BrokenProcessPool above.
            last_exc = e
            if attempt == 0 and not _cancel_requested:
                continue
            raise
        except Exception as e:
            last_exc = e
            if _is_stale_fd_error(e) and attempt == 0 and not _cancel_requested:
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
    on_heartbeat: Callable[[Any], None] | None = None,
) -> T:
    from concurrent.futures.process import BrokenProcessPool

    grace = first_beat_grace_seconds if first_beat_grace_seconds is not None else stall_seconds
    # Per-submit Pipe instead of a long-lived Manager Queue. The
    # previous Manager.Queue path leaked Unix-socket FDs and tripped
    # `_posixsubprocess.fork_exec` with "bad value(s) in fds_to_keep"
    # after enough PDFs; raw Pipe + explicit close-in-finally keeps
    # the parent's FD set bounded.
    ctx = mp.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    pool = _get_pool()
    future = pool.submit(fn, child_conn, *args)

    start = time.monotonic()
    last_beat: float | None = None
    poll_seconds = max(0.05, min(1.0, stall_seconds / 4.0))
    try:
        while not future.done():
            try:
                if parent_conn.poll(poll_seconds):
                    beat = parent_conn.recv()
                    last_beat = time.monotonic()
                    if on_heartbeat is not None:
                        # Bubble per-page beats up to the ETA so a long
                        # PDF refines remaining time as pages tick.
                        with _suppress(Exception):
                            on_heartbeat(beat)
            except (EOFError, OSError):
                break

            if future.done():
                break

            now = time.monotonic()
            if last_beat is not None:
                baseline, threshold = last_beat, stall_seconds
            else:
                baseline, threshold = start, grace
            if now - baseline > threshold:
                _trace(
                    f"stall-raise threshold={threshold:.1f}s "
                    f"first_beat={last_beat is not None} arg={args[0] if args else '?'}"
                )
                _kill_pool_workers(pool)
                shutdown_pool()
                raise StallError(f"no heartbeat for {threshold:.1f}s; worker killed")

        try:
            return future.result()
        except BrokenProcessPool:
            _trace(f"future-broken arg={args[0] if args else '?'}")
            shutdown_pool()
            raise
    finally:
        with _suppress(Exception):
            parent_conn.close()
        with _suppress(Exception):
            child_conn.close()

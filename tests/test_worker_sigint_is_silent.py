"""The tty's ^C reaches the extraction worker too (same process group); it must die silently."""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Generator
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX tty signals")


def test_sigint_ends_the_idle_worker_without_a_traceback(capfd: pytest.CaptureFixture[str]) -> None:
    """A SIGINT to the idle worker ends it by signal, writing nothing to the shared stderr."""
    from fnd.extract._worker import _get_pool, _noop_worker, shutdown_pool, warm_pool  # pyright: ignore[reportPrivateUsage]

    shutdown_pool()
    try:
        warm_pool()
        _get_pool().submit(_noop_worker).result(timeout=30)
        (proc,) = list(_get_pool()._processes.values())  # pyright: ignore[reportPrivateUsage]
        assert proc.pid is not None
        os.kill(proc.pid, signal.SIGINT)
        proc.join(timeout=10)
        assert proc.exitcode == -signal.SIGINT
    finally:
        shutdown_pool()
    assert "KeyboardInterrupt" not in capfd.readouterr().err


@contextlib.contextmanager
def _sigint_mid_boot(
    spawn: Callable[[], None],
    settle: Callable[[], None],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Generator[int | None]:
    """Run ``spawn``, ^C the worker it starts while it boots, and yield its exit code;
    ``settle`` ends whatever ``spawn`` started before the pool goes."""
    from fnd.extract._worker import _get_pool, shutdown_pool  # pyright: ignore[reportPrivateUsage]

    # The worker opens its trace file before its initializer, and a FIFO with no
    # reader holds it there: the line arrives only if the ^C did not end the boot.
    fifo = tmp_path / "boot"
    os.mkfifo(fifo)
    monkeypatch.setenv("FND_WORKER_TRACE", str(fifo))
    shutdown_pool()
    trace: int | None = None
    try:
        spawn()
        procs = _get_pool()._processes  # pyright: ignore[reportPrivateUsage]
        deadline = time.monotonic() + 10
        while not procs and time.monotonic() < deadline:
            time.sleep(0.01)
        (proc,) = list(procs.values())
        assert proc.pid is not None
        os.kill(proc.pid, signal.SIGINT)
        trace = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        proc.join(timeout=10)
        assert b"module-imported" in os.read(trace, 65536)
        yield proc.exitcode
    finally:
        # The reader outlives the pool: a worker that reaches the FIFO with no
        # reader blocks in open() for ever, and shutdown waits on it.
        if trace is None:
            trace = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        settle()
        shutdown_pool()
        os.close(trace)


def test_sigint_while_the_worker_boots_ends_it_without_a_traceback(
    capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``warm_pool`` returns before the worker is up; a ^C meanwhile still ends it silently."""
    from fnd.extract._worker import warm_pool

    with _sigint_mid_boot(warm_pool, lambda: None, monkeypatch, tmp_path) as exitcode:
        assert exitcode == -signal.SIGINT
    assert "KeyboardInterrupt" not in capfd.readouterr().err


def test_sigint_while_a_respawned_worker_boots_ends_it_without_a_traceback(
    capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A worker respawned from an extraction thread boots as quietly as the warmed one."""
    from fnd.extract import _worker

    outcome: list[BaseException] = []

    def extract() -> None:
        try:
            _worker.run_in_pool_sync_with_stall_detection(
                _worker.collect_pdf_chunks_with_heartbeat, tmp_path / "a.pdf", False
            )
        except BaseException as e:
            outcome.append(e)

    thread = threading.Thread(target=extract, daemon=True)
    with _sigint_mid_boot(
        thread.start, lambda: thread.join(timeout=15), monkeypatch, tmp_path
    ) as exitcode:
        assert exitcode == -signal.SIGINT
    assert not thread.is_alive()
    assert isinstance(outcome[0], _worker.WorkerInterrupted)
    assert "KeyboardInterrupt" not in capfd.readouterr().err


def test_sigint_mid_extraction_does_not_respawn_the_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ^C that kills a busy worker ends the extraction instead of retrying it on a fresh one."""
    from fnd.extract import _worker

    monkeypatch.setenv("FND_TEST_SLOW_EXTRACT_SECONDS", "30")
    _worker.shutdown_pool()
    _worker.clear_cancel()
    started = threading.Event()
    outcome: list[BaseException] = []

    def extract() -> None:
        try:
            _worker.run_in_pool_sync_with_stall_detection(
                _worker.collect_pdf_chunks_with_heartbeat,
                tmp_path / "slow.pdf",
                False,
                on_heartbeat=lambda _beat: started.set(),
            )
        except BaseException as e:
            outcome.append(e)

    thread = threading.Thread(target=extract, daemon=True)
    try:
        thread.start()
        assert started.wait(timeout=30)
        (proc,) = list(_worker._get_pool()._processes.values())  # pyright: ignore[reportPrivateUsage]
        assert proc.pid is not None
        os.kill(proc.pid, signal.SIGINT)
        thread.join(timeout=15)
        assert not thread.is_alive()
        assert isinstance(outcome[0], _worker.WorkerInterrupted)
    finally:
        _worker.clear_cancel()
        _worker.shutdown_pool()

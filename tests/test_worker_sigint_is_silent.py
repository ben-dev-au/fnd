"""The tty's ^C reaches the extraction worker too (same process group); it must die silently."""

from __future__ import annotations

import os
import signal
import sys
import threading
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


def test_sigint_while_the_worker_boots_ends_it_without_a_traceback(
    capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``warm_pool`` returns before the worker is up; a ^C meanwhile still ends it silently."""
    from fnd.extract._worker import _get_pool, shutdown_pool, warm_pool  # pyright: ignore[reportPrivateUsage]

    # The worker opens its trace file before its initializer, and a FIFO with no
    # reader holds it there: the line arrives only if the ^C did not end the boot.
    fifo = tmp_path / "boot"
    os.mkfifo(fifo)
    monkeypatch.setenv("FND_WORKER_TRACE", str(fifo))
    shutdown_pool()
    try:
        warm_pool()
        (proc,) = list(_get_pool()._processes.values())  # pyright: ignore[reportPrivateUsage]
        assert proc.pid is not None
        os.kill(proc.pid, signal.SIGINT)
        trace = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        try:
            proc.join(timeout=10)
            assert b"module-imported" in os.read(trace, 65536)
        finally:
            os.close(trace)
        assert proc.exitcode == -signal.SIGINT
    finally:
        shutdown_pool()
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

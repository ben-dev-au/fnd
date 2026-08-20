"""The stall watch must fire, name what was running, and stay off by default.

A diagnostic that silently stops reporting is worse than none — it turns "no
stalls logged" into evidence when it is really an absence of evidence. So the
tests here are that it arms only when asked, that a genuinely blocked loop
produces a line, and that the line carries the attribution that makes it useful.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from fnd.tui.stall_watch import StallWatch


class _FakePreview:
    coverage_activity: str | None = "abc12345/seq7"

    def user_mount_in_flight(self) -> bool:
        return True

    def pipeline_busy(self) -> bool:
        return False


class _FakeScroll:
    is_settling = False


class _FakeLazy:
    task = None


class _FakeQueue:
    def qsize(self) -> int:
        return 3


class _FakePrefetch:
    active_job = "struct:14e238f4"
    sink_queue = _FakeQueue()


class _FakeApp:
    """Only what the watch reads — it must not need a running TUI to work."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self._preview = _FakePreview()
        self._preview_scroll = _FakeScroll()
        self._lazy = _FakeLazy()
        self._prefetch = _FakePrefetch()

    def _diag_log(self, msg: str) -> None:
        self.lines.append(msg)


def test_off_unless_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("_FND_STALL_WATCH", raising=False)
    assert StallWatch.from_env(_FakeApp()) is None  # type: ignore[arg-type]
    monkeypatch.setenv("_FND_STALL_WATCH", "0")
    assert StallWatch.from_env(_FakeApp()) is None  # type: ignore[arg-type]


def test_the_threshold_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("_FND_STALL_WATCH", "250")
    watch = StallWatch.from_env(_FakeApp())  # type: ignore[arg-type]
    assert watch is not None
    assert watch._threshold_ms == 250

    # ``=1`` means "on", not "report everything over a millisecond".
    monkeypatch.setenv("_FND_STALL_WATCH", "1")
    watch = StallWatch.from_env(_FakeApp())  # type: ignore[arg-type]
    assert watch is not None
    assert watch._threshold_ms > 1


@pytest.mark.asyncio
async def test_a_blocked_loop_is_reported_with_what_was_running() -> None:
    app = _FakeApp()
    watch = StallWatch(app, threshold_ms=150)  # type: ignore[arg-type]
    watch.start()
    await asyncio.sleep(0.1)
    # Block the loop the way a capture does — synchronously, no awaits — and
    # then FINISH, clearing the marker before the watch can wake. That is what
    # real work does, and it is why sampling only on waking named the successor
    # and exonerated the culprit every time.
    time.sleep(0.4)
    app._preview.coverage_activity = None
    await asyncio.sleep(0.15)
    watch.stop()

    stalls = [line for line in app.lines if line.startswith("STALL")]
    assert stalls, f"a 400ms block went unreported; log was {app.lines}"
    # The culprit has finished by the time the watch wakes, so the line has to
    # carry the state from before the loop went away as well as after.
    # The CPU figure is what separates a blocked loop from a process the OS
    # simply stopped running; without it the two are indistinguishable.
    assert "cpu=" in stalls[0], f"stall line carries no CPU figure: {stalls[0]}"
    assert "before[" in stalls[0], f"no pre-stall state: {stalls[0]}"
    assert "after[" in stalls[0], f"no post-stall state: {stalls[0]}"
    before_part = stalls[0].split("before[", 1)[1].split("]", 1)[0]
    assert "capturing=abc12345/seq7" in before_part, (
        f"the pre-stall snapshot does not say what was running: {stalls[0]}"
    )
    assert "mount=True" in before_part, "pre-stall snapshot lost the pipeline state"
    # Prefetch mounts widgets on the loop and no other flag covers it, which is
    # why real stalls kept being logged with everything else False.
    assert "prefetch=struct:14e238f4/q3" in before_part, (
        f"the pre-stall snapshot does not say what prefetch was doing: {stalls[0]}"
    )
    # And the post-stall half must show the culprit already gone, which is
    # exactly the reading that made the old single-sample line useless.
    after_part = stalls[0].split("after[", 1)[1].rsplit("]", 1)[0]
    assert "capturing=None" in after_part, (
        f"the post-stall snapshot should show the finished capture cleared: {stalls[0]}"
    )


@pytest.mark.asyncio
async def test_cpu_time_separates_real_work_from_a_sleeping_process() -> None:
    """The number that makes a stall line actionable.

    Work holding the loop burns CPU for most of the gap; a process the OS
    stopped running burns none. Wall-clock alone cannot tell them apart, which
    is how a 7.7s gap the user never felt got logged like a real freeze.
    """
    app = _FakeApp()
    watch = StallWatch(app, threshold_ms=150)  # type: ignore[arg-type]
    watch.start()
    await asyncio.sleep(0.1)
    # A BUSY block — real work, not a sleep.
    spin_until = time.perf_counter() + 0.4
    while time.perf_counter() < spin_until:
        pass
    await asyncio.sleep(0.15)
    watch.stop()

    stalls = [line for line in app.lines if line.startswith("STALL")]
    assert stalls, f"a 400ms busy block went unreported: {app.lines}"
    cpu_ms = float(stalls[0].split("cpu=", 1)[1].split("ms", 1)[0])
    assert cpu_ms > 200, (
        f"work that spun for 400ms reported only {cpu_ms}ms of CPU — the figure "
        f"cannot separate a blocked loop from an idle one: {stalls[0]}"
    )


@pytest.mark.asyncio
async def test_a_responsive_loop_reports_nothing() -> None:
    app = _FakeApp()
    watch = StallWatch(app, threshold_ms=150)  # type: ignore[arg-type]
    watch.start()
    for _ in range(12):
        await asyncio.sleep(0.02)
    watch.stop()
    assert not [line for line in app.lines if line.startswith("STALL")], (
        f"reported a stall on an idle loop: {app.lines}"
    )


def test_the_sample_key_survives_windows_path_separators() -> None:
    """The sampler filters frames by path and then takes basenames.

    Both assumed `/`. On Windows every filename comes back with backslashes, so
    the filter matched nothing and every sample keyed off the same empty string:
    unrelated stalls merged into one bucket and the diagnostic reported nothing,
    silently, on the platform where attaching a debugger is hardest.
    """
    import traceback

    from fnd.tui.stall_watch import stack_key

    def _stack(*frames: tuple[str, str]) -> traceback.StackSummary:
        return traceback.StackSummary.from_list(
            [(filename, 1, name, "") for filename, name in frames]
        )

    posix = _stack(
        ("/home/x/.venv/lib/textual/app.py", "_process_messages"),
        ("/home/x/fnd/tui/preview/presenter.py", "_mount_chunks_async"),
    )
    windows = _stack(
        (r"C:\Users\x\.venv\Lib\textual\app.py", "_process_messages"),
        (r"C:\Users\x\fnd\tui\preview\presenter.py", "_mount_chunks_async"),
    )

    expected = "presenter.py:_mount_chunks_async < app.py:_process_messages"
    assert stack_key(posix) == expected
    assert stack_key(windows) == expected, (
        "a Windows stack keyed off something other than its frames — "
        "unnormalised separators drop every frame and collapse to ''"
    )

    # Frames outside our code are dropped on both, so the key stays readable.
    assert stack_key(_stack((r"C:\Python\Lib\asyncio\events.py", "_run"))) == ""

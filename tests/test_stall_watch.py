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


class _FakeApp:
    """Only what the watch reads — it must not need a running TUI to work."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self._preview = _FakePreview()
        self._preview_scroll = _FakeScroll()
        self._lazy = _FakeLazy()

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
    # Block the loop the way a capture does — synchronously, no awaits.
    time.sleep(0.4)
    await asyncio.sleep(0.15)
    watch.stop()

    stalls = [line for line in app.lines if line.startswith("STALL")]
    assert stalls, f"a 400ms block went unreported; log was {app.lines}"
    assert "capturing=abc12345/seq7" in stalls[0], (
        f"stall line does not say what was running: {stalls[0]}"
    )
    assert "mount=True" in stalls[0], "stall line lost the pipeline state"


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

"""Install/uninstall progress modal — modal-lifecycle tests.

Follows dev/docs/test_patterns/settings_screen.md §9: task lives on
FNDApp, Background dismisses but task survives, Cancel SIGTERMs the
subprocess.

Uses a fake-command chain (``sleep`` from ``/bin/sleep``) so the
subprocess actually runs but doesn't touch the venv. The point is to
exercise lifecycle, not real package installation.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import cast

import pytest
from textual.widgets import ProgressBar

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp
from tests._pilot_wait import wait_until


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.fixture
def cfg_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "config.toml"
    p.write_text("")
    monkeypatch.setattr("fnd.config.default_config_path", lambda: p)
    return p


@pytest.fixture
def cfg(cfg_path: Path) -> Config:
    return load(cfg_path)


@pytest.fixture
def fake_sleep() -> list[list[str]]:
    """A 2-command chain that ``sleep``s for a fraction of a second each.
    Lets pilot tests reach the running state without blocking the test
    suite."""
    sleep = shutil.which("sleep") or "/bin/sleep"
    return [[sleep, "0.05"], [sleep, "0.05"]]


# 1 — Modal mounts and renders chrome


@pytest.mark.asyncio
async def test_progress_modal_chrome(
    built_index: Path, cfg: Config, fake_sleep: list[list[str]]
) -> None:
    from fnd.tui.extras_install_progress import (
        ExtrasInstallProgressScreen,
        start_extras_install,
    )

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        ok = start_extras_install(app, cmds=fake_sleep, action_label="Install")
        assert ok is True
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ExtrasInstallProgressScreen)
        assert screen.query_one("#extras_box")
        assert screen.query_one("#extras_progress", ProgressBar)
        # Title now lives on the box's border_title (set in compose).
        box = screen.query_one("#extras_box")
        title = str(getattr(box, "border_title", "") or "")
        assert "Install" in title
        assert "pdf-structure" in title
        # Wait for completion to avoid leaking the task into the next test.
        assert app._extras_task is not None
        await asyncio.wait_for(app._extras_task, timeout=3.0)


# 2 — Background dismisses modal, task survives


@pytest.mark.asyncio
async def test_background_keeps_task_running(
    built_index: Path, cfg: Config, fake_sleep: list[list[str]]
) -> None:
    """A long-running chain dismissed via Esc leaves the task on the
    app; reopening the modal reattaches."""
    sleep = shutil.which("sleep") or "/bin/sleep"
    cmds = [[sleep, "0.3"], [sleep, "0.3"]]

    from fnd.tui.extras_install_progress import (
        ExtrasInstallProgressScreen,
        start_extras_install,
    )

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        start_extras_install(app, cmds=cmds, action_label="Install")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        # Modal gone; task still alive.
        assert not isinstance(app.screen, ExtrasInstallProgressScreen)
        assert app._extras_task is not None
        assert not app._extras_task.done()
        await asyncio.wait_for(app._extras_task, timeout=2.0)


# 3 — Cancel stops the subprocess


@pytest.mark.asyncio
async def test_cancel_stops_subprocess(built_index: Path, cfg: Config) -> None:
    """A long sleep gets cut short by Cancel. Subprocess receives SIGTERM
    via _extras_cancel + send_signal."""
    sleep = shutil.which("sleep") or "/bin/sleep"
    cmds = [[sleep, "30"]]

    from fnd.tui.extras_install_progress import start_extras_install

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        start_extras_install(app, cmds=cmds, action_label="Install")
        # Wait until the subprocess has actually spawned (event, not a fixed
        # sleep) so Cancel has a live proc to SIGTERM.
        await wait_until(
            pilot,
            lambda: app._extras_proc is not None,
            timeout=5.0,
            message="extras subprocess never spawned",
        )
        # Send Cancel via the binding.
        await pilot.press("c")
        # The task should complete fairly quickly after cancel.
        assert app._extras_task is not None
        await asyncio.wait_for(app._extras_task, timeout=3.0)
        # And the proc handle should be released.
        assert app._extras_proc is None


# 4 — Idempotent: second start while running re-attaches modal


@pytest.mark.asyncio
async def test_double_start_reattaches(built_index: Path, cfg: Config) -> None:
    """A second start while one is in flight re-attaches instead of re-running.

    Uses the long-sleep-then-cancel shape of ``test_cancel_stops_subprocess``
    rather than a short sleep. The old ``sleep 0.5`` raced the pauses below:
    on a loaded runner it exited first, so the second call correctly started a
    fresh run and returned True. That failed as ``assert True is False`` on
    Linux CI while macOS and Windows passed — the test's premise had lapsed,
    not the behaviour under test.
    """
    sleep = shutil.which("sleep") or "/bin/sleep"
    cmds = [[sleep, "30"]]

    from fnd.tui.extras_install_progress import (
        ExtrasInstallProgressScreen,
        start_extras_install,
    )

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        ok1 = start_extras_install(app, cmds=cmds, action_label="Install")
        assert ok1 is True
        # Gate on the spawn event, not a fixed pause.
        await wait_until(
            pilot,
            lambda: app._extras_proc is not None,
            timeout=5.0,
            message="extras subprocess never spawned",
        )
        await pilot.press("escape")
        await pilot.pause()
        # State the premise the next assertion depends on, so a lapsed window
        # fails here with a clear message instead of as "assert True is False".
        assert app._extras_task is not None
        assert not app._extras_task.done(), "first run finished before the second start"

        ok2 = start_extras_install(app, cmds=cmds, action_label="Install")
        assert ok2 is False
        await pilot.pause()
        assert isinstance(app.screen, ExtrasInstallProgressScreen)

        # Cancel rather than waiting out the sleep.
        await pilot.press("c")
        await asyncio.wait_for(app._extras_task, timeout=5.0)


# 5 — Worker emits done event on success


@pytest.mark.asyncio
async def test_worker_emits_done(cfg: Config) -> None:
    """Direct unit test of run_install — bypass the modal."""
    from fnd.tui.extras_install_progress import ProgressEvent, run_install

    sleep = shutil.which("sleep") or "/bin/sleep"
    cancel = asyncio.Event()
    events: asyncio.Queue[ProgressEvent] = asyncio.Queue()

    class _FakeApp:
        _extras_proc = None

    await run_install(
        cast(FNDApp, _FakeApp()), cmds=[[sleep, "0.02"]], cancel=cancel, events=events
    )

    phases: list[str] = []
    while not events.empty():
        phases.append(events.get_nowait().phase)
    assert "starting" in phases
    assert "running" in phases
    assert phases[-1] == "done"


# 6 — Worker emits failed on missing executable


@pytest.mark.asyncio
async def test_worker_emits_failed_on_missing_executable() -> None:
    from fnd.tui.extras_install_progress import ProgressEvent, run_install

    cancel = asyncio.Event()
    events: asyncio.Queue[ProgressEvent] = asyncio.Queue()

    class _FakeApp:
        _extras_proc = None

    await run_install(
        cast(FNDApp, _FakeApp()),
        cmds=[["/this/binary/does/not/exist", "x"]],
        cancel=cancel,
        events=events,
    )
    phases: list[str] = []
    while not events.empty():
        phases.append(events.get_nowait().phase)
    assert "failed" in phases

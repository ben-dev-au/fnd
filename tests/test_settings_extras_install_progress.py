"""Install/uninstall progress modal — modal-lifecycle tests.

Follows docs/test_patterns/settings_screen.md §9: task lives on
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
from textual.widgets import ProgressBar, Static

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp


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
        # Title carries action label.
        title = str(screen.query_one("#extras_title", Static).content)
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
        # Give the subprocess a moment to spawn.
        await pilot.pause(0.2)
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
    sleep = shutil.which("sleep") or "/bin/sleep"
    cmds = [[sleep, "0.5"]]

    from fnd.tui.extras_install_progress import (
        ExtrasInstallProgressScreen,
        start_extras_install,
    )

    app = FNDApp(index_dir=built_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        ok1 = start_extras_install(app, cmds=cmds, action_label="Install")
        assert ok1 is True
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        # Second call while task is still alive.
        ok2 = start_extras_install(app, cmds=cmds, action_label="Install")
        assert ok2 is False
        await pilot.pause()
        assert isinstance(app.screen, ExtrasInstallProgressScreen)
        assert app._extras_task is not None
        await asyncio.wait_for(app._extras_task, timeout=3.0)


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

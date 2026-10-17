"""A failed or cancelled install leaves the machine as it found it; Windows removals wait."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fnd import config as conf
from fnd import extras, extras_change, os_labels, paths, semantic
from fnd.cli import app
from fnd.extras import SEMANTIC
from fnd.extras_change import Change, finish_pending_removals, pending_removal, remove_folders

_PYPROJECT = (
    '[dependency-groups]\nsemantic = ["onnxruntime"]\n\n[tool.uv]\ndefault-groups = ["dev"]\n'
)


class _Machine:
    """Installed distributions and a command runner that changes them."""

    def __init__(self, fail_on: tuple[str, ...] = ("uv", "sync")) -> None:
        self.dists = {"fndr", "rich"}
        self.ran: list[list[str]] = []
        self.fail_on = fail_on

    def run(self, argv: list[str]) -> tuple[int, str, str]:
        self.ran.append(argv)
        if argv[1:4] == ["-m", "fnd.semantic", "fetch"]:
            folder = semantic.model_dir(semantic.GRANITE)
            folder.mkdir(parents=True)
            (folder / "model.onnx").write_bytes(b"weights")
            return 0, "", ""
        if argv[:3] == ["uv", "pip", "uninstall"]:
            self.dists -= set(argv[5:])
            return 0, "", ""
        if tuple(argv[: len(self.fail_on)]) == self.fail_on:
            self.dists |= {"onnxruntime", "numpy"}
            return 1, "", "network down"
        return 0, "", ""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(_PYPROJECT, encoding="utf-8")
    monkeypatch.setattr(extras, "project_pyproject_for_python", lambda py: pyproject)
    monkeypatch.setattr(extras, "fnd_tool_venv", lambda: False)
    monkeypatch.setattr(semantic, "platform_supported", lambda *a: True)
    monkeypatch.setattr("fnd.cli._require_uv", lambda: None)
    return pyproject


@pytest.fixture
def machine(monkeypatch: pytest.MonkeyPatch) -> _Machine:
    m = _Machine()
    monkeypatch.setattr(extras, "run_command", m.run)
    monkeypatch.setattr(extras, "installed_names", lambda: set(m.dists))
    return m


def test_a_failed_install_puts_everything_back(project: Path, machine: _Machine) -> None:
    """The model, the packages it added and the project's groups are as they were."""
    before = project.read_bytes()
    result = CliRunner().invoke(app, ["extras", "install", "semantic", "--yes"])
    assert result.exit_code == 1
    assert not semantic.model_dir(semantic.GRANITE).exists()
    assert machine.dists == {"fndr", "rich"}
    assert project.read_bytes() == before
    assert conf.load().defaults.semantic_search is False
    assert "Removed what it added" in result.output


def test_rollback_keeps_an_edit_made_to_pyproject_meanwhile(
    project: Path, machine: _Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the group membership is undone, not the whole file."""

    def edit_then_fail(argv: list[str]) -> tuple[int, str, str]:
        if argv[:2] == ["uv", "sync"]:
            project.write_text(project.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
        return machine.run(argv)

    monkeypatch.setattr(extras, "run_command", edit_then_fail)
    CliRunner().invoke(app, ["extras", "install", "semantic", "--yes"])
    text = project.read_text(encoding="utf-8")
    assert "# edited" in text
    assert not extras.in_default_groups(project, "semantic")


def test_ctrl_c_during_an_install_puts_everything_back(
    project: Path, machine: _Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted install is rolled back too, and exits 130."""

    def interrupt(argv: list[str]) -> tuple[int, str, str]:
        if argv[:2] == ["uv", "sync"]:
            raise KeyboardInterrupt
        return machine.run(argv)

    monkeypatch.setattr(extras, "run_command", interrupt)
    result = CliRunner().invoke(app, ["extras", "install", "semantic", "--yes"])
    assert result.exit_code == 130
    assert not semantic.model_dir(semantic.GRANITE).exists()
    assert 'default-groups = ["dev"]' in project.read_text(encoding="utf-8")


def test_a_model_already_there_is_kept(
    project: Path, machine: _Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rollback removes only what this install brought."""
    folder = semantic.model_dir(semantic.GRANITE)
    folder.mkdir(parents=True)
    machine_run = machine.run

    def fetch_is_a_no_op(argv: list[str]) -> tuple[int, str, str]:
        if argv[1:4] == ["-m", "fnd.semantic", "fetch"]:
            return 0, "", ""
        return machine_run(argv)

    monkeypatch.setattr(extras, "run_command", fetch_is_a_no_op)
    CliRunner().invoke(app, ["extras", "install", "semantic", "--yes"])
    assert folder.exists()


def test_a_successful_install_keeps_what_it_added(project: Path, machine: _Machine) -> None:
    """Negative control: nothing is undone when every step succeeds."""
    machine.fail_on = ("never",)
    result = CliRunner().invoke(app, ["extras", "install", "semantic", "--yes"])
    assert result.exit_code == 0, result.output
    assert semantic.model_dir(semantic.GRANITE).exists()
    assert '"semantic"' in project.read_text(encoding="utf-8")
    assert conf.load().defaults.semantic_search is True


@pytest.mark.asyncio
async def test_the_tui_rolls_back_a_failed_step() -> None:
    """A failing command runs the rollback and the failure says what it undid."""
    from types import SimpleNamespace

    from fnd.tui.extras_install_progress import ProgressEvent, run_install

    events: asyncio.Queue[ProgressEvent] = asyncio.Queue()
    calls: list[str] = []
    await run_install(
        SimpleNamespace(),  # type: ignore[arg-type]
        cmds=[["false"]],
        cancel=asyncio.Event(),
        events=events,
        roll_back=lambda: calls.append("rolled") or "Nothing was kept.",
    )
    last = [events.get_nowait() for _ in range(events.qsize())][-1]
    assert calls == ["rolled"]
    assert last.phase == "failed"
    assert "Nothing was kept." in last.error


@pytest.mark.asyncio
async def test_the_tui_rolls_back_a_cancelled_install() -> None:
    """Cancelling mid-chain rolls back rather than leaving half an install."""
    from types import SimpleNamespace

    from fnd.tui.extras_install_progress import ProgressEvent, run_install

    events: asyncio.Queue[ProgressEvent] = asyncio.Queue()
    cancel = asyncio.Event()
    cancel.set()
    calls: list[str] = []
    await run_install(
        SimpleNamespace(),  # type: ignore[arg-type]
        cmds=[["true"]],
        cancel=cancel,
        events=events,
        roll_back=lambda: calls.append("rolled") or "Nothing was kept.",
    )
    last = [events.get_nowait() for _ in range(events.qsize())][-1]
    assert calls == ["rolled"]
    assert last.phase == "cancelled"
    assert last.line == "Nothing was kept."


def _windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os_labels, "is_windows", lambda: True)
    monkeypatch.setattr(paths, "loaded_files_are_locked", lambda: True)
    monkeypatch.setattr(extras, "project_pyproject_for_python", lambda py: None)
    monkeypatch.setattr(extras, "fnd_tool_venv", lambda: False)
    monkeypatch.setattr(extras, "is_package_installed", lambda pkg: True)
    monkeypatch.setattr(extras, "recorded_additions", lambda name: {"onnxruntime"})


def _queued_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The vectors folder still open (Windows): deleting it now leaves it, so it is queued."""
    from fnd.vectors import store_dir

    folder = store_dir(semantic.GRANITE)
    folder.mkdir(parents=True)
    real_rmtree = extras_change.shutil.rmtree
    monkeypatch.setattr(extras_change.shutil, "rmtree", lambda *a, **k: None)
    assert remove_folders("semantic", [folder]) is False
    monkeypatch.setattr(extras_change.shutil, "rmtree", real_rmtree)
    return folder


def test_removing_loaded_packages_on_windows_runs_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """Measured on Windows 11: uv removes a package fnd has loaded, so nothing waits."""
    import sys
    import types

    _windows(monkeypatch)
    monkeypatch.setitem(sys.modules, "onnxruntime", types.ModuleType("onnxruntime"))
    change = Change.begin(SEMANTIC, removing=True)
    assert change.commands[0][:3] == ["uv", "pip", "uninstall"]
    assert not pending_removal("semantic")


def test_a_queue_names_only_the_extras_own_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An old-shape or hand-edited queue never deletes anything else, relative paths included."""
    work = tmp_path / "cwd"
    for name in ("commands", "folders", "elsewhere"):
        (work / name).mkdir(parents=True)
        (work / name / "precious.txt").write_text("keep", encoding="utf-8")
    monkeypatch.chdir(work)
    queue = paths.app_data_dir() / "extras" / "pending-removal.json"
    queue.parent.mkdir(parents=True)
    for shape in (
        {"semantic": {"commands": [["uv", "x"]], "folders": ["folders"]}},
        {"semantic": [str(work / "elsewhere"), "commands"]},
        {"no-such-extra": [str(work / "elsewhere")]},
        ["not", "a", "mapping"],
    ):
        queue.write_text(json.dumps(shape), encoding="utf-8")
        assert finish_pending_removals() == []
        assert not pending_removal("semantic")
    assert all((work / n / "precious.txt").exists() for n in ("commands", "folders", "elsewhere"))


def test_the_tui_says_when_a_folder_waits_for_the_next_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The done line follows what the removal actually managed."""
    from types import SimpleNamespace

    from fnd.tui import settings_screen

    monkeypatch.setattr(extras_change, "remove_folders", lambda name, folders: False)
    monkeypatch.setattr("fnd.tui.menu._setting_writer", lambda path: lambda app, v: None)
    change = Change(
        extra=SEMANTIC,
        removing=True,
        commands=[],
        files={},
        folders=frozenset(),
        dists=frozenset(),
        tools=frozenset(),
    )
    app = SimpleNamespace(_extras_done_note=SEMANTIC.removed_note)
    settings_screen.extras_finished(app, change)  # type: ignore[arg-type]
    assert app._extras_done_note == "Its last files go when fnd next starts."


def test_a_folder_still_open_is_removed_at_the_next_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder that cannot be deleted now is queued and deleted later."""
    folder = _queued_folder(tmp_path, monkeypatch)
    assert pending_removal("semantic")
    assert finish_pending_removals() == ["semantic"]
    assert not folder.exists()


def test_a_folder_still_open_at_the_next_start_stays_queued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another fnd may still map it; the queue waits for a later start."""
    _queued_folder(tmp_path, monkeypatch)
    monkeypatch.setattr(extras_change.shutil, "rmtree", lambda *a, **k: None)
    assert finish_pending_removals() == []
    assert pending_removal("semantic")


def test_reinstalling_before_the_restart_cancels_the_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise the next start would delete the model just downloaded."""
    _windows(monkeypatch)
    _queued_folder(tmp_path, monkeypatch)
    Change.begin(SEMANTIC, removing=False)
    assert not pending_removal("semantic")


def test_fnd_finishes_pending_removals_when_it_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any fnd start deletes what an earlier removal could not."""
    import sys

    from fnd import cli

    folder = _queued_folder(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["fnd", "version"])
    with pytest.raises(SystemExit):
        cli.main()
    assert not folder.exists()
    assert not pending_removal("semantic")


def test_a_pending_record_fnd_cannot_rewrite_does_not_stop_it_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The folders still go; the record is retried at the next start."""
    import sys

    from fnd import cli

    folder = _queued_folder(tmp_path, monkeypatch)

    def locked(pending: object) -> None:
        raise PermissionError("in use")

    monkeypatch.setattr(extras_change, "_save_pending", locked)
    monkeypatch.setattr(sys, "argv", ["fnd", "version"])
    with pytest.raises(SystemExit) as exit_:
        cli.main()
    assert exit_.value.code in (0, None)
    assert capsys.readouterr().out.strip()
    assert not folder.exists()
    assert not pending_removal("semantic")
    assert finish_pending_removals() == []


def test_a_windows_uv_tool_install_hands_over_its_own_reinstall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fnd cannot rebuild the venv it runs from on Windows; it says what to run after quitting."""
    _windows(monkeypatch)
    monkeypatch.setattr(extras, "fnd_tool_venv", lambda: True)
    monkeypatch.setattr(extras, "fnd_version", lambda: "1.0.1")
    install = Change.begin(SEMANTIC, removing=False)
    assert not any(c[:3] == ["uv", "tool", "install"] for c in install.commands)
    assert install.commands
    assert install.after_quit.startswith("uv tool install --reinstall fndr[semantic]==1.0.1")
    removal = Change.begin(SEMANTIC, removing=True)
    assert removal.commands == []
    assert not pending_removal("semantic")
    assert removal.after_quit.startswith("uv tool install --reinstall fndr==1.0.1")


def test_elsewhere_a_uv_tool_install_runs_its_reinstall(monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative control: off Windows the reinstall runs in place."""
    _windows(monkeypatch)
    monkeypatch.setattr(paths, "loaded_files_are_locked", lambda: False)
    monkeypatch.setattr(extras, "fnd_tool_venv", lambda: True)
    monkeypatch.setattr(extras, "fnd_version", lambda: "1.0.1")
    install = Change.begin(SEMANTIC, removing=False)
    assert any(c[:3] == ["uv", "tool", "install"] for c in install.commands)
    assert install.after_quit == ""


def test_the_cli_says_what_to_run_after_quitting(
    project: Path, machine: _Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows under uv tool, the install fetches the model and prints the reinstall to run."""
    machine.fail_on = ("never",)
    monkeypatch.setattr(extras, "project_pyproject_for_python", lambda py: None)
    monkeypatch.setattr(os_labels, "is_windows", lambda: True)
    monkeypatch.setattr(paths, "loaded_files_are_locked", lambda: True)
    monkeypatch.setattr(extras, "fnd_tool_venv", lambda: True)
    monkeypatch.setattr(extras, "fnd_version", lambda: "1.0.1")
    result = CliRunner().invoke(app, ["extras", "install", "semantic", "--yes"])
    assert result.exit_code == 0, result.output
    assert (
        "Quit every open fnd, then run: uv tool install --reinstall fndr[semantic]==1.0.1"
        in result.output
    )
    assert not any(c[:3] == ["uv", "tool", "install"] for c in machine.ran)


@pytest.mark.asyncio
async def test_the_tui_says_what_to_run_after_quitting(monkeypatch: pytest.MonkeyPatch) -> None:
    """The modal's closing note carries the command."""
    from types import SimpleNamespace

    from fnd.tui import settings_screen

    seen: dict[str, object] = {}
    monkeypatch.setattr(
        "fnd.tui.extras_install_progress.start_extras_install",
        lambda app, **kw: seen.update(kw) or True,
    )
    change = Change(
        extra=SEMANTIC,
        removing=False,
        commands=[],
        files={},
        folders=frozenset(),
        dists=frozenset(),
        tools=frozenset(),
        after_quit="uv tool install --reinstall 'fndr[semantic]==1.0.1'",
    )
    await settings_screen.change_extra(SimpleNamespace(), change, label="Install")
    assert (
        seen["done_note"]
        == "Quit every open fnd, then run: uv tool install --reinstall 'fndr[semantic]==1.0.1'"
    )


def test_confirming_while_another_install_runs_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The running install's modal comes back; this change is not begun, so nothing is left half-done."""
    from types import SimpleNamespace

    from fnd.tui import settings_screen

    begun: list[object] = []
    monkeypatch.setattr(Change, "begin", classmethod(lambda cls, *a, **k: begun.append(a)))
    reattached: list[bool] = []
    monkeypatch.setattr(
        "fnd.tui.extras_install_progress.reattach", lambda app: reattached.append(True) or True
    )
    assert settings_screen.begin_change(SimpleNamespace(), SEMANTIC, removing=False) is None
    assert begun == []
    assert reattached == [True]


@pytest.mark.asyncio
async def test_a_reattached_modal_keeps_the_running_extras_title() -> None:
    """Reopening shows the install that is actually running, not the one just asked for."""
    import shutil as _shutil

    from fnd.config import Config
    from fnd.tui import FNDApp
    from fnd.tui.extras_install_progress import reattach, start_extras_install

    sleep = _shutil.which("sleep") or "/bin/sleep"
    app = FNDApp(index_dir=Path(paths.app_data_dir()) / "index", config=Config())
    async with app.run_test(size=(120, 32)) as pilot:
        start_extras_install(
            app, cmds=[[sleep, "1"]], action_label="Install", extra_name="pdf-structure"
        )
        await pilot.pause()
        app.pop_screen()
        assert reattach(app) is True
        await pilot.pause()
        assert str(app.screen.query_one("#extras_box").border_title) == "Install pdf-structure"
        await asyncio.wait_for(app._extras_task, timeout=5.0)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_failed_removal_reloads_what_it_released() -> None:
    """Removal let go of the model; if it fails, search must get the model back."""
    from types import SimpleNamespace

    from fnd.tui.extras_install_progress import ProgressEvent, run_install

    events: asyncio.Queue[ProgressEvent] = asyncio.Queue()
    calls: list[str] = []
    await run_install(
        SimpleNamespace(),  # type: ignore[arg-type]
        cmds=[["false"]],
        cancel=asyncio.Event(),
        events=events,
        on_failed=lambda: calls.append("reloaded"),
    )
    assert calls == ["reloaded"]


def test_settings_says_a_removal_is_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While a folder waits for the next start, Settings does not call semantic installed."""
    from types import SimpleNamespace

    from fnd.tui import menu

    monkeypatch.setattr(semantic, "platform_supported", lambda *a: True)
    monkeypatch.setattr(menu, "_semantic_ready", lambda: True)
    _queued_folder(tmp_path, monkeypatch)
    text = menu._semantic_status_text(SimpleNamespace())  # type: ignore[arg-type]  # pyright: ignore[reportPrivateUsage]
    assert text == "✗ Removed · its last files go when fnd next starts"


@pytest.mark.asyncio
async def test_quitting_mid_install_stops_it_and_rolls_back() -> None:
    """Quit ends a running install's command and undoes it, rather than leaving it to finish alone."""
    import shutil as _shutil

    from fnd.config import Config
    from fnd.tui import FNDApp
    from fnd.tui.extras_install_progress import start_extras_install

    sleep = _shutil.which("sleep") or "/bin/sleep"
    calls: list[str] = []
    app = FNDApp(index_dir=Path(paths.app_data_dir()) / "index", config=Config())
    async with app.run_test(size=(120, 32)) as pilot:
        start_extras_install(
            app,
            cmds=[[sleep, "30"]],
            action_label="Install",
            extra_name="semantic",
            roll_back=lambda: calls.append("rolled") or "Nothing was kept.",
        )
        for _ in range(100):
            await pilot.pause(0.05)
            if app._extras_proc is not None:  # type: ignore[attr-defined]
                break
        proc = app._extras_proc  # type: ignore[attr-defined]
        assert proc is not None
        app.exit()
    assert calls == ["rolled"]
    assert proc.returncode is not None


@pytest.mark.asyncio
async def test_quitting_during_a_rollback_does_not_start_a_second() -> None:
    """A rollback already running owns the undo; quit does not run it again alongside."""
    import threading
    from types import SimpleNamespace

    from fnd.tui.extras_install_progress import ProgressEvent, run_install, stop_for_quit

    release = threading.Event()
    calls: list[str] = []

    def slow_roll_back() -> str:
        calls.append("rolled")
        release.wait(5)
        return "Nothing was kept."

    app = SimpleNamespace(_extras_roll_back=slow_roll_back, _extras_proc=None)
    events: asyncio.Queue[ProgressEvent] = asyncio.Queue()
    task = asyncio.create_task(
        run_install(
            app,  # type: ignore[arg-type]
            cmds=[["false"]],
            cancel=asyncio.Event(),
            events=events,
            roll_back=slow_roll_back,
        )
    )
    app._extras_task = task  # type: ignore[attr-defined]
    while not calls:
        await asyncio.sleep(0.01)
    await stop_for_quit(app)  # type: ignore[arg-type]
    release.set()
    await asyncio.sleep(0.05)
    assert calls == ["rolled"]


def test_the_command_to_run_is_quoted_for_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """cmd.exe passes single quotes through, so a Windows command is quoted its own way."""
    monkeypatch.setattr(os_labels.platform, "system", lambda: "Windows")
    assert os_labels.command_line(["uv", "tool", "install", "fndr[semantic]==1.0.1"]) == (
        "uv tool install fndr[semantic]==1.0.1"
    )
    assert os_labels.command_line(["uv", "x", "a b"]) == 'uv x "a b"'
    monkeypatch.setattr(os_labels.platform, "system", lambda: "Darwin")
    assert os_labels.command_line(["uv", "x", "a b"]) == "uv x 'a b'"


def test_a_cli_uninstall_says_both_what_to_run_and_what_waits(
    machine: _Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reinstall to run after quitting does not hide the folder left for the next start."""
    _windows(monkeypatch)
    monkeypatch.setattr(semantic, "platform_supported", lambda *a: True)
    monkeypatch.setattr("fnd.cli._require_uv", lambda: None)
    monkeypatch.setattr(extras, "fnd_tool_venv", lambda: True)
    monkeypatch.setattr(extras, "fnd_version", lambda: "1.0.1")
    monkeypatch.setattr(extras_change, "remove_folders", lambda name, folders: False)
    semantic.model_dir(semantic.GRANITE).mkdir(parents=True)
    result = CliRunner().invoke(app, ["extras", "uninstall", "semantic", "--yes"])
    assert "Quit every open fnd, then run: uv tool install --reinstall fndr==1.0.1" in result.output
    assert "its last files go when fnd next starts" in result.output


def test_the_semantic_disclosure_says_nothing_about_pdfs(project: Path, machine: _Machine) -> None:
    """Found on Windows: the install text ended with pdf-structure's flat-text line."""
    result = CliRunner().invoke(app, ["extras", "install", "semantic", "--dry-run"])
    assert "Model: granite" in result.output
    assert "PDFs" not in result.output

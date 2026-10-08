"""Each platform's rule lives in its seam, and every one runs on every OS."""

from __future__ import annotations

import ast
from pathlib import Path, PureWindowsPath

import pytest

from fnd.launcher import LinuxLauncher, MacLauncher, WindowsLauncher

CODE_CMD = "C:\\Users\\me\\AppData\\Local\\Programs\\VS Code\\bin\\code.cmd"


@pytest.mark.parametrize("cls", [MacLauncher, LinuxLauncher])
def test_a_posix_editor_splits_as_a_shell_would(
    cls: type[MacLauncher | LinuxLauncher], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VISUAL", "code -w")
    launcher = cls(which=lambda _name: None)
    assert launcher.editor_argv(Path("/x/c.toml")) == ["code", "-w", str(Path("/x/c.toml"))]


@pytest.mark.parametrize("cls", [MacLauncher, LinuxLauncher])
def test_a_posix_editor_path_with_spaces_is_one_program(
    cls: type[MacLauncher | LinuxLauncher], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VISUAL", "/Apps/My Editor/bin/edit")
    launcher = cls(which=lambda name: name if name.startswith("/Apps") else None)
    assert launcher.editor_argv(Path("c.toml"))[0] == "/Apps/My Editor/bin/edit"


@pytest.mark.parametrize("cls", [MacLauncher, LinuxLauncher])
def test_a_posix_default_editor_is_vi(
    cls: type[MacLauncher | LinuxLauncher], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    assert cls(which=lambda _name: None).editor_argv(Path("c.toml"))[0] == "vi"


def test_a_quoted_windows_editor_loses_its_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISUAL", f'"{CODE_CMD}" --wait')
    launcher = WindowsLauncher(which=lambda _name: None)
    assert launcher.editor_argv(Path("c.toml")) == [CODE_CMD, "--wait", str(Path("c.toml"))]


def test_a_windows_editor_name_resolves_to_its_cmd_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`code` is code.cmd, which runs from its full path but not its bare name."""
    monkeypatch.setenv("VISUAL", "code --wait")
    launcher = WindowsLauncher(which=lambda name: CODE_CMD if name == "code" else None)
    assert launcher.editor_argv(Path("c.toml"))[:2] == [CODE_CMD, "--wait"]


def test_a_one_word_windows_editor_resolves_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISUAL", "code")
    launcher = WindowsLauncher(which=lambda name: CODE_CMD if name == "code" else None)
    assert launcher.editor_argv(Path("c.toml"))[0] == CODE_CMD


def test_a_windows_editor_planted_in_the_current_folder_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VISUAL", "code")
    for found in (str(tmp_path / "code.cmd"), ".\\code.cmd"):
        launcher = WindowsLauncher(which=lambda _name, hit=found: hit)
        assert launcher.editor_argv(Path("c.toml"))[0] == "code", found


@pytest.mark.parametrize("arg", ["C:\\A&B\\c.toml", "C:\\100%\\c.toml", 'C:\\a"b'])
def test_a_batch_editor_is_refused_an_argument_cmd_would_act_on(arg: str) -> None:
    assert not WindowsLauncher().safe_to_run([CODE_CMD, arg])


def test_a_batch_editor_with_a_plain_path_runs() -> None:
    assert WindowsLauncher().safe_to_run([CODE_CMD, "--wait", "C:\\Users\\me\\c.toml"])


def test_an_exe_editor_is_never_refused() -> None:
    assert WindowsLauncher().safe_to_run(["C:\\vim.exe", "C:\\A&B\\c.toml"])


def test_a_windows_default_editor_is_notepad(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    assert WindowsLauncher(which=lambda _name: None).editor_argv(Path("c.toml"))[0] == "notepad"


@pytest.mark.parametrize(
    ("path", "url"),
    [
        ("\\\\server\\share\\a.md", "//server/share/a.md"),
        ("\\\\?\\C:\\Users\\a.md", "/C:/Users/a.md"),
    ],
)
def test_a_share_or_long_windows_path_in_a_url(path: str, url: str) -> None:
    assert WindowsLauncher().url_path(PureWindowsPath(path)) == url  # pyright: ignore[reportArgumentType]


def test_a_windows_path_in_a_url_is_a_drive_url_path() -> None:
    url = WindowsLauncher().url_path(PureWindowsPath("C:\\Users\\me\\a b.md"))  # pyright: ignore[reportArgumentType]
    assert url == "/C:/Users/me/a b.md"


@pytest.mark.parametrize("cls", [MacLauncher, LinuxLauncher])
def test_a_posix_path_in_a_url_is_the_path(cls: type[MacLauncher | LinuxLauncher]) -> None:
    assert cls().url_path(Path("/n/a.md")) == str(Path("/n/a.md"))


def test_a_pasted_posix_path_loses_its_shell_escapes() -> None:
    from fnd.paths import _posix_pasted_path  # pyright: ignore[reportPrivateUsage]

    assert _posix_pasted_path("/Users/me/My\\ Vault\\ \\(old\\)") == "/Users/me/My Vault (old)"


def test_a_pasted_windows_path_keeps_its_backslashes() -> None:
    from fnd.paths import _windows_pasted_path  # pyright: ignore[reportPrivateUsage]

    assert _windows_pasted_path("C:\\notes\\(old)") == "C:\\notes\\(old)"


@pytest.mark.parametrize(
    ("platform", "want"), [("win32", "C:\\a\\ b"), ("linux", "C:\\a b"), ("darwin", "C:\\a b")]
)
def test_the_pasted_path_rule_is_chosen_by_platform(
    platform: str, want: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fnd import paths

    monkeypatch.setattr(paths.sys, "platform", platform)
    assert paths.unescape_pasted_path("C:\\a\\ b") == want


# Files that branched on the platform before the seams existed; a new module
# must route through paths, launcher, os_labels or apps instead.
_PLATFORM_CHECKS_ALLOWED = {
    "apps.py",
    "cache.py",
    "cli.py",
    "cloud_files.py",
    "config.py",
    "extract/_docling_daemon.py",
    "filters/scan.py",
    "fsmeta.py",
    "index.py",
    "launcher.py",
    "opener.py",
    "os_labels.py",
    "paths.py",
    "tags.py",
    "tui/clipboard.py",
    "tui/menu.py",
    "tui/preview/frozen_store.py",
    "tui/scope_panel.py",
    "walk.py",
}


_PLATFORM_FACTS = {("sys", "platform"), ("os", "name"), ("platform", "system")}


def _branches_on_platform(tree: ast.AST) -> bool:
    """Whether a module reads the platform, however it imported the name."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                aliases[name.asname or name.name] = name.name
        elif isinstance(node, ast.ImportFrom) and any(
            (node.module, n.name) in _PLATFORM_FACTS for n in node.names
        ):
            return True
    return any(
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and (aliases.get(node.value.id, node.value.id), node.attr) in _PLATFORM_FACTS
        for node in ast.walk(tree)
    )


def test_no_new_module_branches_on_the_platform() -> None:
    root = Path(__file__).resolve().parent.parent / "fnd"
    offenders = sorted(
        rel
        for path in root.rglob("*.py")
        if (rel := path.relative_to(root).as_posix()) not in _PLATFORM_CHECKS_ALLOWED
        and _branches_on_platform(ast.parse(path.read_text("utf-8")))
    )
    assert not offenders, offenders


@pytest.mark.parametrize(
    "source",
    ["import sys as _s\nX = _s.platform", "from sys import platform", "import os\nX = os.name"],
)
def test_the_gate_sees_an_aliased_platform_read(source: str) -> None:
    assert _branches_on_platform(ast.parse(source))


def test_edit_refuses_a_batch_editor_before_starting_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from fnd import launcher

    started: list[list[str]] = []
    monkeypatch.setenv("VISUAL", "code")
    monkeypatch.setattr(
        launcher, "get_launcher", lambda: WindowsLauncher(which=lambda _name: CODE_CMD)
    )
    monkeypatch.setattr(launcher.subprocess, "call", lambda argv: started.append(argv) or 0)
    assert launcher.edit(PureWindowsPath("C:\\A&B\\c.toml")) == launcher.LAUNCH_REFUSED  # pyright: ignore[reportArgumentType]
    assert started == []


def test_a_relative_path_entry_counts_as_the_current_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VISUAL", "code")
    launcher = WindowsLauncher(which=lambda _name: "tools\\code.cmd")
    assert launcher.editor_argv(Path("c.toml"))[0] == "code"


def test_a_batch_editor_in_a_folder_cmd_would_act_on_is_refused() -> None:
    assert not WindowsLauncher().safe_to_run(["C:\\Tools&Co\\code.cmd", "C:\\c.toml"])


def test_a_long_form_share_path_in_a_url() -> None:
    url = WindowsLauncher().url_path(PureWindowsPath("\\\\?\\UNC\\server\\share\\a.md"))  # pyright: ignore[reportArgumentType]
    assert url == "//server/share/a.md"


def test_an_app_launch_refuses_a_batch_file_a_file_name_could_drive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user app's {path} is an indexed file name; `x&calc&y.md` must not reach cmd.exe."""
    from fnd import launcher

    started: list[list[str]] = []
    monkeypatch.setattr(launcher, "get_launcher", lambda: WindowsLauncher())
    monkeypatch.setattr(launcher, "_run", lambda argv: started.append(argv) or 0)
    assert launcher.run(["C:\\Apps\\open.cmd", "C:\\notes\\x&calc&y.md"]) == launcher.LAUNCH_REFUSED
    assert started == []


@pytest.mark.parametrize("refused", [True, False])
def test_the_editor_message_names_the_real_reason(refused: bool) -> None:
    from fnd import launcher
    from fnd.tui.config_recovery_screen import _editor_refusal

    rc = launcher.LAUNCH_REFUSED if refused else launcher.LAUNCH_FAILED
    message = _editor_refusal(Path("c.toml"), rc)
    assert ("cmd.exe would act on" in message) is refused
    assert ("on your PATH" in message) is not refused


def test_a_refusal_is_never_a_real_exit_status() -> None:
    """A program's own 126 ("found but not runnable") must not read as a refusal."""
    from fnd import launcher

    assert launcher.LAUNCH_REFUSED not in range(-64, 256)


def test_a_windows_editor_not_found_safely_is_not_started(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare name would make Windows search the current folder first."""
    from fnd import launcher

    started: list[list[str]] = []
    monkeypatch.setenv("VISUAL", "code")
    monkeypatch.setattr(launcher, "get_launcher", lambda: WindowsLauncher(which=lambda _n: None))
    monkeypatch.setattr(launcher.subprocess, "call", lambda argv: started.append(argv) or 0)
    assert launcher.edit(Path("c.toml")) == launcher.LAUNCH_FAILED
    assert started == []


def test_a_windows_hit_in_the_current_folder_by_full_path_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fnd import launcher

    monkeypatch.setattr(launcher.os, "getcwd", lambda: "C:\\work")
    monkeypatch.setenv("VISUAL", "code")
    found = WindowsLauncher(which=lambda _n: "C:\\work\\code.cmd")
    assert found.editor_argv(Path("c.toml"))[0] == "code"


@pytest.mark.parametrize("cls", [MacLauncher, LinuxLauncher])
def test_a_posix_bare_editor_is_found_safely(cls: type[MacLauncher | LinuxLauncher]) -> None:
    assert cls().found_safely(["vi", "c.toml"])


def test_saved_states_names_the_file_where_it_now_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fnd import index_runner

    monkeypatch.setattr(index_runner, "state_dir", lambda: tmp_path)
    (tmp_path / "x..state.toml").write_text(
        '[state]\ncollection = "x."\nstarted_at = "2026-10-01T00:00:00+00:00"\n'
        "total_files = 2\nfiles_completed = 1\n",
        encoding="utf-8",
    )
    ((path, _state),) = index_runner.saved_states()
    assert path.exists()
    assert path == index_runner.state_file_for("x.")


def test_two_state_files_keep_their_own_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finished old-name state beside a live new-name one must not take its path."""
    from fnd import index_runner

    monkeypatch.setattr(index_runner, "state_dir", lambda: tmp_path)
    old = tmp_path / "Ca\N{ZERO WIDTH SPACE}fe.state.toml"
    live = tmp_path / "Cafe.state.toml"
    for f, done, total in ((old, 4, 4), (live, 10, 100)):
        f.write_text(
            f'[state]\ncollection = "{f.name.removesuffix(".state.toml")}"\n'
            f'started_at = "2026-10-01T00:00:00+00:00"\ntotal_files = {total}\n'
            f"files_completed = {done}\n",
            encoding="utf-8",
        )
    paths = {state.files_completed: path for path, state in index_runner.saved_states()}
    assert paths == {4: old, 10: live}

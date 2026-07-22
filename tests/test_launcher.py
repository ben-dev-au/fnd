"""Per-platform unit tests for the OS launcher seam (``fnd.launcher``).

Every branch is exercised by injecting a spy runner / ``startfile`` / ``which``
into the concrete launcher — no process is ever spawned, so these run
identically on any OS (the CI matrix then confirms the real spawns).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd import launcher


class _Spy:
    """Records the argv/target of the last (and all) effect calls."""

    def __init__(self) -> None:
        self.runs: list[list[str]] = []
        self.spawns: list[list[str]] = []
        self.starts: list[str] = []

    def run(self, argv: list[str]) -> int:
        self.runs.append(argv)
        return 0

    def spawn(self, argv: list[str]) -> None:
        self.spawns.append(argv)

    def startfile(self, target: str) -> None:
        self.starts.append(target)


def test_mac_launcher_uses_open() -> None:
    spy = _Spy()
    mac = launcher.MacLauncher(run=spy.run, spawn=spy.spawn)
    mac.open_path(Path("/docs/a.pdf"))
    mac.open_url("skim:///x#page=3")
    mac.reveal(Path("/docs/a.pdf"))
    assert spy.runs == [["open", "/docs/a.pdf"], ["open", "skim:///x#page=3"]]
    assert spy.spawns == [["open", "-R", "/docs/a.pdf"]]


def test_linux_launcher_uses_xdg_open() -> None:
    spy = _Spy()
    lin = launcher.LinuxLauncher(run=spy.run, spawn=spy.spawn, which=lambda _b: None)
    lin.open_path(Path("/docs/a.pdf"))
    lin.open_url("obsidian://open?path=/x")
    assert spy.runs == [["xdg-open", "/docs/a.pdf"], ["xdg-open", "obsidian://open?path=/x"]]


def test_linux_reveal_selects_when_file_manager_present() -> None:
    spy = _Spy()
    lin = launcher.LinuxLauncher(
        run=spy.run,
        spawn=spy.spawn,
        which=lambda b: "/usr/bin/nautilus" if b == "nautilus" else None,
    )
    lin.reveal(Path("/docs/sub/a.pdf"))
    assert spy.spawns == [["nautilus", "--select", "/docs/sub/a.pdf"]]


def test_linux_reveal_falls_back_to_parent_dir() -> None:
    spy = _Spy()
    lin = launcher.LinuxLauncher(run=spy.run, spawn=spy.spawn, which=lambda _b: None)
    lin.reveal(Path("/docs/sub/a.pdf"))
    assert spy.spawns == [["xdg-open", str(Path("/docs/sub"))]]


def test_windows_launcher_uses_startfile_and_explorer() -> None:
    spy = _Spy()
    win = launcher.WindowsLauncher(startfile=spy.startfile, spawn=spy.spawn)
    win.open_path(Path(r"C:\docs\a.pdf"))
    win.open_url("obsidian://open?path=x")
    win.reveal(Path(r"C:\docs\a.pdf"))
    assert spy.starts == [r"C:\docs\a.pdf", "obsidian://open?path=x"]
    assert spy.spawns == [["explorer", "/select,", r"C:\docs\a.pdf"]]


def test_open_path_returns_runner_code() -> None:
    lin = launcher.LinuxLauncher(
        run=lambda _argv: 7, spawn=lambda _argv: None, which=lambda _b: None
    )
    assert lin.open_path(Path("/x")) == 7
    assert lin.open_url("u") == 7


@pytest.mark.parametrize(
    ("system", "cls"),
    [
        ("Darwin", "MacLauncher"),
        ("Windows", "WindowsLauncher"),
        ("Linux", "LinuxLauncher"),
        ("FreeBSD", "LinuxLauncher"),  # any non-mac/win POSIX → xdg-open family
    ],
)
def test_factory_picks_platform_launcher(
    monkeypatch: pytest.MonkeyPatch, system: str, cls: str
) -> None:
    monkeypatch.setattr("fnd.launcher.platform.system", lambda: system)
    launcher.get_launcher.cache_clear()
    got = launcher.get_launcher()
    assert type(got).__name__ == cls
    launcher.get_launcher.cache_clear()

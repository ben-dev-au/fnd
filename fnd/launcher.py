"""OS launcher — open a file/URL in the desktop's default handler and reveal
a file in the platform file manager, isolated behind one seam.

Mirrors :mod:`fnd.tui.clipboard`: the OS-specific choice is made once by
:func:`get_launcher`, and each concrete launcher takes its process runner
(and, on Windows, ``os.startfile``) as injectable dependencies, so every
branch is unit-testable without spawning anything.

Deep-linking to a page/line is deliberately *not* here — that is a per-app
concern owned by the :mod:`fnd.apps` handlers. This seam only covers "hand
it to the OS default" (:meth:`Launcher.open_path`), "hand this URL scheme to
the OS" (:meth:`Launcher.open_url`), and "show the file in the file manager"
(:meth:`Launcher.reveal`).

``open_path`` / ``open_url`` block briefly and return the launch return code
(the OS opener hands off to the desktop and returns immediately); ``reveal``
is fire-and-forget so the TUI never stalls on file-manager launch latency.
"""

from __future__ import annotations

import contextlib
import functools
import os
import platform
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path, PureWindowsPath
from shutil import which
from typing import Protocol, runtime_checkable

Runner = Callable[[list[str]], int]
Spawner = Callable[[list[str]], None]
StartFile = Callable[[str], None]

# Non-zero code returned when a launch can't even start (missing opener binary,
# no OS handler for the type). The API contract is return-code-only — a UI
# action handler must never have to catch an exception from an open/reveal.
LAUNCH_FAILED = 127

# Not started, deliberately: a batch file would hand its command line to cmd.exe.
# Outside every exit status a process can return (0 to 255, a negative signal,
# a large positive Windows code), so a program's own 126 is never read as this.
LAUNCH_REFUSED = -256
REFUSED_REASON = (
    "it is a batch file (.cmd or .bat), and its command line holds a character "
    'cmd.exe would act on: & % ^ | < > ! "'
)


def _run(argv: list[str]) -> int:
    """Blocking launch; DEVNULL so a chatty opener (xdg-open) can't bleed
    into the TUI's screen. A missing opener binary returns ``LAUNCH_FAILED``
    rather than raising ``FileNotFoundError`` into the caller."""
    try:
        return subprocess.run(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
        ).returncode
    except OSError:
        return LAUNCH_FAILED


def _spawn(argv: list[str]) -> None:
    """Non-blocking launch, output discarded. Fire-and-forget: a missing binary
    is swallowed so a failed reveal never raises into the TUI."""
    with contextlib.suppress(OSError):
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@runtime_checkable
class Launcher(Protocol):
    """Open/reveal operations every platform must provide."""

    def open_path(self, path: Path) -> int: ...
    def open_url(self, url: str) -> int: ...
    def reveal(self, path: Path) -> None: ...
    def editor_argv(self, path: Path) -> list[str]: ...
    def url_path(self, path: Path) -> str: ...
    def safe_to_run(self, argv: list[str]) -> bool: ...
    def found_safely(self, argv: list[str]) -> bool: ...


def _editor_value() -> str:
    return (os.environ.get("VISUAL") or os.environ.get("EDITOR") or "").strip()


class _PosixCommands:
    """What macOS and Linux share: a shell's word splitting and ``/`` paths."""

    _which: Callable[[str], str | None]

    def editor_argv(self, path: Path) -> list[str]:
        raw = _editor_value()
        if raw and self._which(raw):
            # The whole value names a program: an unquoted path holding spaces.
            return [raw, str(path)]
        try:
            argv = shlex.split(raw)
        except ValueError:
            argv = [raw]
        return [*(argv or ["vi"]), str(path)]

    def url_path(self, path: Path) -> str:
        return str(path)

    def safe_to_run(self, argv: list[str]) -> bool:
        """Always: an argv list reaches no shell here."""
        return True

    def found_safely(self, argv: list[str]) -> bool:
        """Always: exec searches PATH only, never the current folder."""
        return True


class MacLauncher(_PosixCommands):
    """macOS ``open`` / ``open -R``."""

    def __init__(
        self,
        *,
        run: Runner = _run,
        spawn: Spawner = _spawn,
        which: Callable[[str], str | None] = which,
    ) -> None:
        self._run = run
        self._spawn = spawn
        self._which = which

    def open_path(self, path: Path) -> int:
        return self._run(["open", str(path)])

    def open_url(self, url: str) -> int:
        return self._run(["open", url])

    def reveal(self, path: Path) -> None:
        self._spawn(["open", "-R", str(path)])


class LinuxLauncher(_PosixCommands):
    """Freedesktop ``xdg-open``, with a best-effort file-manager ``--select``
    for reveal (falling back to opening the containing folder)."""

    # File managers that reliably support selecting a file, tried in order.
    _SELECTORS: tuple[tuple[str, str], ...] = (
        ("nautilus", "--select"),
        ("dolphin", "--select"),
    )

    def __init__(
        self,
        *,
        run: Runner = _run,
        spawn: Spawner = _spawn,
        which: Callable[[str], str | None] = which,
    ) -> None:
        self._run = run
        self._spawn = spawn
        self._which = which

    def open_path(self, path: Path) -> int:
        return self._run(["xdg-open", str(path)])

    def open_url(self, url: str) -> int:
        return self._run(["xdg-open", url])

    def reveal(self, path: Path) -> None:
        for binary, flag in self._SELECTORS:
            if self._which(binary):
                self._spawn([binary, flag, str(path)])
                return
        # No selecting file manager available — open the containing folder.
        self._spawn(["xdg-open", str(path.parent)])


class WindowsLauncher:
    """Windows ``os.startfile`` (default handler) / ``explorer /select,``."""

    def __init__(
        self,
        *,
        startfile: StartFile | None = None,
        spawn: Spawner = _spawn,
        which: Callable[[str], str | None] = which,
    ) -> None:
        # ``os.startfile`` only exists on Windows and is resolved lazily (at
        # call time) so the class type-checks, imports, and constructs on
        # every platform — the factory builds it before any call, and tests
        # inject a fake ``startfile`` when running off-Windows.
        self._startfile = startfile
        self._spawn = spawn
        self._which = which

    def _start(self, target: str) -> int:
        startfile = self._startfile
        if startfile is None:
            # os.startfile exists only on Windows; WindowsLauncher runs there
            # in production (tests inject a fake), so this attribute access is
            # safe despite type-checkers flagging it off-Windows.
            startfile = os.startfile  # type: ignore[attr-defined]
        try:
            startfile(target)  # raises OSError when the type has no handler
        except OSError:
            return LAUNCH_FAILED
        return 0

    def open_path(self, path: Path) -> int:
        return self._start(str(path))

    def open_url(self, url: str) -> int:
        return self._start(url)

    def reveal(self, path: Path) -> None:
        # explorer returns exit code 1 even on success; fire-and-forget.
        self._spawn(["explorer", "/select,", str(path)])

    def _resolve(self, name: str) -> str | None:
        """``name`` on PATH by Windows' own search (PATHEXT finds `code.cmd` for
        `code`), never from the current folder, where a file can be planted."""
        found = self._which(name)
        if found is None:
            return None
        # Windows path rules whatever the host, so tests run on every OS. A
        # relative hit (`.\code.cmd`, `tools\code.cmd`) resolves into the cwd.
        hit = PureWindowsPath(found)
        if not hit.is_absolute() or hit.parent == PureWindowsPath(os.getcwd()):
            return None
        return found

    def editor_argv(self, path: Path) -> list[str]:
        raw = _editor_value()
        whole = self._resolve(raw) if raw else None
        if whole is not None:
            return [whole, str(path)]
        try:
            argv = shlex.split(raw, posix=False)
        except ValueError:
            argv = [raw]
        argv = [a[1:-1] if len(a) > 1 and a[0] == a[-1] == '"' else a for a in argv]
        argv = argv or ["notepad"]
        # A full path to a .cmd starts where the bare name does not.
        return [self._resolve(argv[0]) or argv[0], *argv[1:], str(path)]

    def url_path(self, path: Path) -> str:
        """``/C:/Users/a.md`` for a drive path, ``//server/share/a.md`` for a share."""
        text = str(path)
        if text.startswith("\\\\?\\UNC\\"):
            text = "\\\\" + text[len("\\\\?\\UNC\\") :]
        text = text.removeprefix("\\\\?\\")
        win = PureWindowsPath(text)
        posix = win.as_posix()
        return f"/{posix}" if len(win.drive) == 2 and win.drive.endswith(":") else posix

    def found_safely(self, argv: list[str]) -> bool:
        """Only a full path: Windows looks for a bare name in the current folder
        first, where :meth:`_resolve` declined a planted file."""
        return bool(argv) and PureWindowsPath(argv[0]).is_absolute()

    def safe_to_run(self, argv: list[str]) -> bool:
        """A batch file runs through cmd.exe, which acts on these anywhere in its
        command line, the program's own path included."""
        if not argv or PureWindowsPath(argv[0]).suffix.lower() not in (".cmd", ".bat"):
            return True
        return not any(set(arg) & _CMD_SPECIAL for arg in argv)


@functools.lru_cache(maxsize=1)
def get_launcher() -> Launcher:
    """Return the launcher for the current OS (cached for the process)."""
    system = platform.system()
    if system == "Darwin":
        return MacLauncher()
    if system == "Windows":
        return WindowsLauncher()
    return LinuxLauncher()


# ── Module-level convenience wrappers ────────────────────────────────────


def open_path(path: Path) -> int:
    """Open ``path`` in the OS default handler for its type."""
    return get_launcher().open_path(path)


def open_url(url: str) -> int:
    """Open a URL (scheme handler) in the OS default handler."""
    return get_launcher().open_url(url)


def reveal(path: Path | str) -> None:
    """Reveal ``path`` in the platform file manager (fire-and-forget)."""
    get_launcher().reveal(Path(path))


def run(argv: list[str]) -> int:
    """Run an app's own command line: blocking, output discarded, never raising,
    and never a batch file handed text its shell would act on."""
    if not get_launcher().safe_to_run(argv):
        return LAUNCH_REFUSED
    return _run(argv)


def capture(argv: list[str], *, timeout: float) -> int:
    """Exit status of a probe, or ``LAUNCH_FAILED`` if it cannot start or finish in time."""
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout, check=False).returncode
    except (OSError, subprocess.TimeoutExpired):
        return LAUNCH_FAILED


# What cmd.exe interprets in a batch file's arguments, quoted or not.
_CMD_SPECIAL = frozenset('&|<>^%!"')


def editor_argv(path: Path) -> list[str]:
    """``$VISUAL``, else ``$EDITOR``, split as this platform's shell would
    (``code -w`` is a program and a flag), then ``path``."""
    return get_launcher().editor_argv(path)


def url_path(path: Path) -> str:
    """``path`` as the path part of a URL on this platform, not yet encoded."""
    return get_launcher().url_path(path)


def edit(path: Path) -> int:
    """Edit ``path`` in the user's editor in the foreground: ``LAUNCH_FAILED``
    if it cannot be found or started, ``LAUNCH_REFUSED`` if starting it is unsafe."""
    argv = editor_argv(path)
    if not get_launcher().found_safely(argv):
        return LAUNCH_FAILED
    if not get_launcher().safe_to_run(argv):
        return LAUNCH_REFUSED
    try:
        return subprocess.call(argv)
    except OSError:
        return LAUNCH_FAILED

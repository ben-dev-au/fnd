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
import subprocess
from collections.abc import Callable
from pathlib import Path
from shutil import which
from typing import Protocol, runtime_checkable

Runner = Callable[[list[str]], int]
Spawner = Callable[[list[str]], None]
StartFile = Callable[[str], None]

# Non-zero code returned when a launch can't even start (missing opener binary,
# no OS handler for the type). The API contract is return-code-only — a UI
# action handler must never have to catch an exception from an open/reveal.
LAUNCH_FAILED = 127


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


class MacLauncher:
    """macOS ``open`` / ``open -R``."""

    def __init__(self, *, run: Runner = _run, spawn: Spawner = _spawn) -> None:
        self._run = run
        self._spawn = spawn

    def open_path(self, path: Path) -> int:
        return self._run(["open", str(path)])

    def open_url(self, url: str) -> int:
        return self._run(["open", url])

    def reveal(self, path: Path) -> None:
        self._spawn(["open", "-R", str(path)])


class LinuxLauncher:
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
    ) -> None:
        # ``os.startfile`` only exists on Windows and is resolved lazily (at
        # call time) so the class type-checks, imports, and constructs on
        # every platform — the factory builds it before any call, and tests
        # inject a fake ``startfile`` when running off-Windows.
        self._startfile = startfile
        self._spawn = spawn

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

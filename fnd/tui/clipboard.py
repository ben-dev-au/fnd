"""System-clipboard writes, isolated behind one seam.

Each OS clipboard tool is described as an argv (``clipboard_argv``) — a
pure resolver that unit-tests per platform. ``copy_text`` takes both the
resolver and the process runner as injectable dependencies, so callers can
exercise the copy path without spawning anything.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from shutil import which

# Ordered POSIX fallbacks, tried until one is installed.
_POSIX_BACKENDS: tuple[list[str], ...] = (
    ["wl-copy"],
    ["xclip", "-selection", "clipboard"],
)

ArgvResolver = Callable[[str], "list[str] | None"]
PipeRunner = Callable[["list[str]", bytes], int]


def clipboard_argv(system: str) -> list[str] | None:
    """Clipboard command for a ``platform.system()`` value, or None if there
    is no usable backend. POSIX returns the first installed of wl-copy/xclip."""
    if system == "Darwin":
        return ["pbcopy"]
    if system == "Windows":
        return ["clip"]
    for argv in _POSIX_BACKENDS:
        if which(argv[0]):
            return list(argv)
    return None


def _run_pipe(argv: list[str], data: bytes) -> int:
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE)
    proc.communicate(input=data)
    return proc.returncode or 0


def copy_text(
    text: str,
    *,
    argv_for: ArgvResolver = clipboard_argv,
    run: PipeRunner = _run_pipe,
) -> None:
    """Write ``text`` to the system clipboard. Raises ``OSError`` when no
    backend is available or the write fails."""
    import platform

    argv = argv_for(platform.system())
    if argv is None:
        raise OSError("no clipboard backend available")
    code = run(argv, text.encode("utf-8"))
    if code:
        raise OSError(f"{argv[0]} exited with {code}")

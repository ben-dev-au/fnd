"""Mermaid render worker: one JSON request per stdin line, one reply per stdout line.

Run by path under ``python -P`` (see :mod:`fnd.tui.mermaid_render`), so it imports
termaid and the standard library only: never fnd, whose TUI import costs a second.
termaid's layout can loop without end on a crafted diagram and grow memory at
about 280 MB/s, so a watchdog thread exits the process past ``argv[1]`` bytes of
resident memory, or once the parent is gone; the parent enforces the time limit.
"""

from __future__ import annotations

import importlib
import json
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from typing import TextIO

_POLL_S = 0.05


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _MemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetCurrentProcess.argtypes = ()
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.K32GetProcessMemoryInfo.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_MemoryCounters),
        wintypes.DWORD,
    )
    _kernel32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
    _SYNCHRONIZE = 0x00100000

    def _probes(parent: int) -> tuple[Callable[[], int], Callable[[], bool]]:
        # os.getppid() keeps a Windows parent's id after it exits: wait on its handle.
        handle = _kernel32.OpenProcess(_SYNCHRONIZE, False, parent)
        me = _kernel32.GetCurrentProcess()

        def resident() -> int:
            counters = _MemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            _kernel32.K32GetProcessMemoryInfo(me, ctypes.byref(counters), counters.cb)
            return int(counters.PeakWorkingSetSize)

        def parent_gone() -> bool:
            return not handle or _kernel32.WaitForSingleObject(handle, 0) == 0

        return resident, parent_gone

else:
    import resource

    def _probes(parent: int) -> tuple[Callable[[], int], Callable[[], bool]]:
        # Linux keeps ru_maxrss across exec, and a vfork spawn hands over fnd's own
        # high-water mark, so read the current figure there; macOS resets it.
        page = os.sysconf("SC_PAGE_SIZE")

        def resident() -> int:
            try:
                with open("/proc/self/statm", "rb") as statm:
                    return int(statm.read().split()[1]) * page
            except OSError:
                return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

        return resident, lambda: os.getppid() != parent


def _watchdog(limit: int, parent: int) -> None:
    resident, parent_gone = _probes(parent)
    while True:
        time.sleep(_POLL_S)
        if resident() > limit or parent_gone():
            os._exit(3)


def _render(request: dict[str, object]) -> dict[str, object]:
    import termaid

    try:
        out = termaid.render_rich(
            str(request["source"]),
            theme=str(request.get("theme", "default")),
            use_ascii=bool(request.get("use_ascii", False)),
        )
    except Exception:
        return {"art": None}
    if not out.plain.strip() or len(out.plain) > int(str(request.get("max_chars", 0))):
        return {"art": None}
    spans = [[s.start, s.end, str(s.style)] for s in out.spans]
    return {"art": {"plain": out.plain, "style": str(out.style), "spans": spans}}


def main() -> None:
    limit, parent = int(sys.argv[1]), int(sys.argv[2])
    # It shares fnd's process group: fnd owns Ctrl+C, and a Ctrl+Z that stopped a
    # render would let the parent's clock run out on a diagram that was fine.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if hasattr(signal, "SIGTSTP"):
        signal.signal(signal.SIGTSTP, signal.SIG_IGN)
    threading.Thread(target=_watchdog, args=(limit, parent), daemon=True).start()
    # Replies own the real stdout; anything termaid prints goes to stderr instead.
    replies = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
    os.dup2(2, 1)
    importlib.import_module("termaid")  # before "ready": a request pays render time only
    _reply(replies, {"ready": True})
    for line in sys.stdin:
        _reply(replies, _render(json.loads(line)))


def _reply(replies: TextIO, message: dict[str, object]) -> None:
    replies.write(json.dumps(message) + "\n")
    replies.flush()


if __name__ == "__main__":
    main()

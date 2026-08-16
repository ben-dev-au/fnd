"""Live unit count for the flat-preview decode.

The flat path (PDF, TXT) builds its whole document in one worker call, so
from the outside it is a single opaque step — and its duration varies by
more than an order of magnitude (measured on a real corpus: p25 226 ms,
median 1081 ms, p75 3135 ms). Nothing observable at dispatch predicts
that: file size scales the duration by roughly its fourth root, because
the preview only ever mounts a window regardless of how big the file is.

So the progress line cannot estimate this path. It has to be told. The
renderer walks a known number of lines, which is the real unit of work,
and reports as it goes.

Same shape as :mod:`fnd.tui.live_progress`, which does this for PDF page
extraction: the worker writes, the UI polls a snapshot. Deliberately not
a callback into the app — this runs on a worker thread, and marshalling
per line would cost more than the work being measured.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

_lock = threading.Lock()


@dataclass(slots=True)
class _State:
    token: int = 0
    done: int = 0
    total: int = 0


_state = _State()


def begin(token: int, total: int) -> None:
    """Start counting ``total`` lines for the load identified by ``token``.

    The token is the caller's own generation counter. A superseded decode is
    still running when its successor starts, and without it the loser's
    reports would land on the winner's count.
    """
    with _lock:
        _state.token = token
        _state.done = 0
        _state.total = max(0, total)


def advance(token: int, done: int) -> None:
    """Report ``done`` lines rendered so far. Ignored once superseded."""
    with _lock:
        if token == _state.token:
            _state.done = done


def snapshot(token: int) -> tuple[int, int]:
    """``(done, total)`` for ``token``, or ``(0, 0)`` when it is not current."""
    with _lock:
        if token != _state.token:
            return (0, 0)
        return (_state.done, _state.total)


def reset() -> None:
    with _lock:
        _state.token = 0
        _state.done = 0
        _state.total = 0


__all__ = ["advance", "begin", "reset", "snapshot"]

"""Two-press Ctrl+C escape hatch for the Textual app.

Textual normally clears ``ISIG`` in termios so ``^C`` arrives as a byte
on stdin, handled via a regular key binding. That binding can only fire
when the event loop is responsive — if a coroutine ever blocks the loop
(slow filesystem walk, hung syscall) the user's ``^C`` does nothing and
the only escape is killing the terminal emulator.

Setting ``TEXTUAL_ALLOW_SIGNALS=1`` tells Textual to leave ``ISIG``
enabled. The tty driver then converts ``^C`` to a real ``SIGINT``, which
the kernel delivers to the Python process even while asyncio is wedged.
We install a Python signal handler that asks the app to exit cleanly on
the first press, then hard-exits on a second press within a short window
so a user who really cannot wait can always get out.
"""

from __future__ import annotations

import contextlib
import math
import os
import signal
import time
from collections.abc import Generator, Iterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from textual.app import App

_HARD_EXIT_WINDOW_S = 1.0
_HARD_EXIT_CODE = 130  # 128 + SIGINT(2), the conventional value
_ENV_VAR = "TEXTUAL_ALLOW_SIGNALS"


def _hard_exit() -> None:
    # ``os._exit`` skips Python finalisers (and Textual's atexit-based
    # terminal restore) but is the only guarantee in a wedged loop. The
    # terminal can be reset with ``stty sane`` if Textual didn't get to
    # tear down the alt-screen; the trade-off is intentional.
    os._exit(_HARD_EXIT_CODE)


def install(app: App[Any]) -> Iterator[None]:
    """Yield a context manager wrapping ``app.run`` with the kill switch.

    Sets ``TEXTUAL_ALLOW_SIGNALS`` so Textual leaves ``ISIG`` on, then
    installs a SIGINT handler that:

    - First press: schedule ``app.exit()`` on the event loop and arm a
      ~1 s window.
    - Second press inside the window: ``os._exit(130)`` so a wedged loop
      can't trap the user.

    Previous environment + handler are restored on exit so a test or a
    repeated launch in the same Python process behaves like a no-op when
    the context manager unwinds.
    """
    prev_env = os.environ.get(_ENV_VAR)
    os.environ[_ENV_VAR] = "1"

    # ``-inf`` sentinel makes the first press unambiguously "outside the
    # window" without needing a separate "first?" flag.
    state: dict[str, float] = {"last_press": -math.inf}

    def _handler(_signum: int, _frame: Any) -> None:
        now = time.monotonic()
        if now - state["last_press"] <= _HARD_EXIT_WINDOW_S:
            _hard_exit()
        state["last_press"] = now
        with contextlib.suppress(Exception):
            app.call_from_thread(app.exit)

    prev_handler = signal.signal(signal.SIGINT, _handler)
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            signal.signal(signal.SIGINT, prev_handler)
        if prev_env is None:
            os.environ.pop(_ENV_VAR, None)
        else:
            os.environ[_ENV_VAR] = prev_env


@contextlib.contextmanager
def kill_switch(app: App[Any]) -> Generator[None]:
    yield from install(app)

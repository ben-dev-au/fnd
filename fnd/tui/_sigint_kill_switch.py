"""Two-press Ctrl+C escape hatch for the Textual app.

Textual normally clears ``ISIG`` in termios so ``^C`` arrives as a byte
on stdin, handled via a regular key binding. That binding can only fire
when the event loop is responsive — if a coroutine ever blocks the loop
(slow filesystem walk, hung syscall, downstream silent crash) the user's
``^C`` does nothing and the only escape is killing the terminal emulator.

Setting ``TEXTUAL_ALLOW_SIGNALS=1`` tells Textual to leave ``ISIG``
enabled. The tty driver then converts ``^C`` to a real ``SIGINT``, which
the kernel delivers to the Python process even while asyncio is wedged.
We install a Python signal handler that asks the app to exit cleanly on
the first press, then unconditionally hard-exits on any subsequent press
so a wedged loop can never trap the user.

Implementation notes:

- Signal handlers run on the **main thread** (the same thread as the
  asyncio loop). Textual's ``call_from_thread`` rejects calls from the
  loop's own thread with ``RuntimeError``. A direct ``app.exit()`` call
  sets the exit flag but doesn't actually unwind the loop — the loop
  has to process its own message queue first, and ``post_message``
  inside a signal-handler frame doesn't reliably do so. The working
  primitive is ``loop.call_soon_threadsafe(app.exit)``: thread-safe by
  design, fires the wakeup pipe, and the loop processes the callback
  on its next iteration.
- The hard-exit threshold is a count, not a time window. Any second
  ``^C`` triggers ``os._exit(130)`` regardless of how slowly the user
  paces presses. A natural double-tap was previously misclassified as
  "still the first press" if the user paused more than a second.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Generator, Iterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from textual.app import App

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

    - First press: schedule ``app.exit`` via
      ``loop.call_soon_threadsafe`` — the only call that reliably fires
      the asyncio wakeup pipe from inside a signal-handler frame on the
      loop's own thread (a direct ``app.exit()`` sets the flag but the
      loop doesn't necessarily notice; ``call_from_thread`` raises). If
      no loop is running yet (or it has already torn down), fall back
      to a direct ``app.exit()`` so the next press still hard-exits.
    - Any subsequent press: ``os._exit(130)``. No timing window — every
      ^C after the first hard-exits, so a wedged loop can never trap the
      user even if they pace presses slowly.

    Previous environment + handler are restored on exit so a test or a
    repeated launch in the same Python process behaves like a no-op when
    the context manager unwinds.
    """
    prev_env = os.environ.get(_ENV_VAR)
    os.environ[_ENV_VAR] = "1"

    state: dict[str, bool] = {"armed": False}

    def _handler(_signum: int, _frame: Any) -> None:
        if state["armed"]:
            _hard_exit()
        state["armed"] = True
        # Same thread as the loop, so call_from_thread errors. Direct
        # app.exit() sets the flag but doesn't reliably wake the loop.
        # call_soon_threadsafe is the one path that fires the wakeup
        # pipe and gets the callback processed promptly.
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon_threadsafe(app.exit)
        except RuntimeError:
            # No running loop (app never started, or already exited).
            # Best-effort direct call so subsequent ^C still hard-exits.
            with contextlib.suppress(Exception):
                app.exit()

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

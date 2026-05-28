"""Two-press Ctrl+C escape hatch — sets TEXTUAL_ALLOW_SIGNALS so the
tty driver converts ^C to SIGINT even from a blocked event loop, then
installs a handler that exits the app cleanly on the first press and
unconditionally hard-exits on any subsequent press.

Empirical findings driving the contract these tests pin down:

- Signal handlers run on the main thread (the asyncio loop's thread).
  Textual's ``call_from_thread`` rejects calls from the loop's own
  thread, so the handler must use ``loop.call_soon_threadsafe(app.exit)``
  instead. A pty-driven probe in dev/ confirmed this is the only call
  that actually unwinds the loop.
- The hard-exit fallback used to be a 1-second time window. Real users
  press ^C 1-3 s apart, so the second press regularly missed the window
  and did nothing. Hard-exit is now triggered on the second press
  unconditionally, no timing dependency.
"""

from __future__ import annotations

import contextlib
import os
import signal
from typing import Any
from unittest.mock import MagicMock, patch

from fnd.tui._sigint_kill_switch import _ENV_VAR, kill_switch


def _stub_app() -> MagicMock:
    return MagicMock()


def test_kill_switch_sets_env_var_inside_block_and_restores() -> None:
    prev = os.environ.pop(_ENV_VAR, None)
    try:
        app = _stub_app()
        with kill_switch(app):
            assert os.environ.get(_ENV_VAR) == "1"
        assert _ENV_VAR not in os.environ
    finally:
        if prev is not None:
            os.environ[_ENV_VAR] = prev


def test_kill_switch_preserves_preexisting_env_var() -> None:
    prev = os.environ.get(_ENV_VAR)
    os.environ[_ENV_VAR] = "preset"
    try:
        app = _stub_app()
        with kill_switch(app):
            assert os.environ[_ENV_VAR] == "1"
        assert os.environ[_ENV_VAR] == "preset"
    finally:
        if prev is None:
            os.environ.pop(_ENV_VAR, None)
        else:
            os.environ[_ENV_VAR] = prev


def test_first_sigint_schedules_app_exit_on_the_loop() -> None:
    """First press: the handler must schedule app.exit via
    loop.call_soon_threadsafe. Calling app.exit directly works only for
    a subset of loop states; call_soon_threadsafe is the reliable path.
    """
    app = _stub_app()
    fake_loop = MagicMock()
    with patch("asyncio.get_event_loop", return_value=fake_loop):
        with kill_switch(app):
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler)
            handler(signal.SIGINT, None)
    fake_loop.call_soon_threadsafe.assert_called_once_with(app.exit)


def test_second_sigint_unconditionally_hard_exits() -> None:
    """Any second ^C — even seconds after the first — must hard-exit.
    Previous time-window logic missed real-world press cadence."""
    app = _stub_app()
    fake_loop = MagicMock()
    with patch("asyncio.get_event_loop", return_value=fake_loop):
        with patch("fnd.tui._sigint_kill_switch._hard_exit") as hard_exit:
            with kill_switch(app):
                handler = signal.getsignal(signal.SIGINT)
                assert callable(handler)
                handler(signal.SIGINT, None)
                hard_exit.assert_not_called()
                handler(signal.SIGINT, None)
            hard_exit.assert_called_once()


def test_handler_falls_back_to_direct_exit_when_no_loop() -> None:
    """If asyncio.get_event_loop raises RuntimeError (app never started
    or already torn down), the handler must still do something so the
    process can exit on the next press."""
    app = _stub_app()
    with patch("asyncio.get_event_loop", side_effect=RuntimeError("no loop")):
        with kill_switch(app):
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler)
            handler(signal.SIGINT, None)
    app.exit.assert_called_once()


def test_kill_switch_restores_previous_handler() -> None:
    sentinel: list[Any] = []

    def prior(_s: int, _f: Any) -> None:
        sentinel.append("prior")

    previous = signal.signal(signal.SIGINT, prior)
    try:
        app = _stub_app()
        with kill_switch(app):
            assert signal.getsignal(signal.SIGINT) is not prior
        assert signal.getsignal(signal.SIGINT) is prior
    finally:
        with contextlib.suppress(Exception):
            signal.signal(signal.SIGINT, previous)


def test_call_soon_threadsafe_arg_is_app_exit_not_a_lambda() -> None:
    """Regression guard: the scheduled callable must be ``app.exit``
    itself so loop.call_soon_threadsafe invokes it with no args. Earlier
    versions wrapped it in ``call_from_thread(app.exit)`` which
    introduced the same-thread RuntimeError that silently failed."""
    app = _stub_app()
    fake_loop = MagicMock()
    with patch("asyncio.get_event_loop", return_value=fake_loop):
        with kill_switch(app):
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler)
            handler(signal.SIGINT, None)
    (scheduled,), _kwargs = fake_loop.call_soon_threadsafe.call_args
    assert scheduled is app.exit

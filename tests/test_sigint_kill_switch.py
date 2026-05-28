"""Two-press Ctrl+C escape hatch — sets TEXTUAL_ALLOW_SIGNALS so the
tty driver converts ^C to SIGINT even from a blocked event loop, then
installs a handler that exits the app cleanly on first press and hard-
exits on a quick second press.
"""

from __future__ import annotations

import contextlib
import os
import signal
from typing import Any
from unittest.mock import MagicMock, patch

from fnd.tui._sigint_kill_switch import _ENV_VAR, kill_switch


def _stub_app() -> MagicMock:
    app = MagicMock()
    # call_from_thread synchronously invokes the callable for testability;
    # in production it bounces through Textual's event loop.
    app.call_from_thread.side_effect = lambda fn, *a, **kw: fn(*a, **kw)
    return app


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


def test_first_sigint_calls_app_exit() -> None:
    app = _stub_app()
    with kill_switch(app):
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        handler(signal.SIGINT, None)
    app.exit.assert_called_once()


def test_second_sigint_within_window_hard_exits() -> None:
    app = _stub_app()
    with patch("fnd.tui._sigint_kill_switch._hard_exit") as hard_exit:
        with kill_switch(app):
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler)
            handler(signal.SIGINT, None)  # first press: graceful
            handler(signal.SIGINT, None)  # second press within window
    hard_exit.assert_called_once()


def test_second_sigint_outside_window_is_graceful_again() -> None:
    app = _stub_app()
    times = iter([0.0, 5.0])  # second press 5s later — outside the 1s window

    def fake_monotonic() -> float:
        return next(times)

    with patch("fnd.tui._sigint_kill_switch.time.monotonic", side_effect=fake_monotonic):
        with patch("fnd.tui._sigint_kill_switch._hard_exit") as hard_exit:
            with kill_switch(app):
                handler = signal.getsignal(signal.SIGINT)
                assert callable(handler)
                handler(signal.SIGINT, None)
                handler(signal.SIGINT, None)
            hard_exit.assert_not_called()
    assert app.exit.call_count == 2


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

"""Committing survives Windows' transient refusal to replace the index metadata.

The fault is injected rather than reproduced: it is another process's timing,
so no platform can be made to produce it on demand.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from fnd.index import commit, commit_async

_WINDOWS_LOCK = ValueError("An IO error occurred: 'Access is denied. (os error 5)'")
_WINDOWS_SHARE = ValueError(
    "An IO error occurred: 'The process cannot access the file because it is "
    "being used by another process. (os error 32)'"
)


class _FlakyWriter:
    """A writer that refuses ``fail_times`` commits the way Windows does."""

    def __init__(self, fail_times: int, error: BaseException = _WINDOWS_LOCK) -> None:
        self.attempts = 0
        self._fail_times = fail_times
        self._error = error

    def commit(self) -> None:
        self.attempts += 1
        if self.attempts <= self._fail_times:
            raise self._error


def _fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the retry ladder's shape, not its 3.15s."""
    monkeypatch.setattr("fnd.index._COMMIT_RETRY_DELAYS", (0.0,) * 6)


def test_commit_retries_a_windows_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    _fast_backoff(monkeypatch)
    writer = _FlakyWriter(fail_times=3)
    commit(writer)  # type: ignore[arg-type]
    assert writer.attempts == 4


def test_commit_retries_a_sharing_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    _fast_backoff(monkeypatch)
    writer = _FlakyWriter(fail_times=1, error=_WINDOWS_SHARE)
    commit(writer)  # type: ignore[arg-type]
    assert writer.attempts == 2


def test_commit_gives_up_and_raises_once_the_ladder_is_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fast_backoff(monkeypatch)
    writer = _FlakyWriter(fail_times=99)
    with pytest.raises(ValueError, match="Access is denied"):
        commit(writer)  # type: ignore[arg-type]
    assert writer.attempts == 7


def test_commit_does_not_retry_an_unrelated_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A schema mismatch or a full disk must surface on the first attempt."""
    _fast_backoff(monkeypatch)
    writer = _FlakyWriter(fail_times=99, error=ValueError("Schema error: field 'body' is missing"))
    with pytest.raises(ValueError, match="Schema error"):
        commit(writer)  # type: ignore[arg-type]
    assert writer.attempts == 1


def test_commit_waits_between_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retrying without a wait re-enters the same lock; the ladder is the fix."""
    monkeypatch.setattr("fnd.index._COMMIT_RETRY_DELAYS", (0.02, 0.02))
    writer = _FlakyWriter(fail_times=2)
    started = time.monotonic()
    commit(writer)  # type: ignore[arg-type]
    assert time.monotonic() - started >= 0.04


@pytest.mark.asyncio
async def test_commit_async_retries_without_blocking_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The indexer runner commits on the event loop, so its waits must yield."""
    monkeypatch.setattr("fnd.index._COMMIT_RETRY_DELAYS", (0.02,) * 6)
    writer = _FlakyWriter(fail_times=2)
    ticks = 0

    async def _count() -> None:
        nonlocal ticks
        import asyncio

        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    import asyncio

    counter: Any = asyncio.ensure_future(_count())
    try:
        await commit_async(writer)  # type: ignore[arg-type]
    finally:
        counter.cancel()
    assert writer.attempts == 3
    assert ticks > 0, "commit_async blocked the loop instead of yielding"

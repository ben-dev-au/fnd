"""Event-driven Pilot wait helpers.

Under heavy suite load, ``pilot.pause()`` can hit Textual's internal
30 s ``_wait_for_screen`` timeout because per-widget ``call_later``
drains stack up faster than the CPU can flush them. Fixed-duration
``pilot.pause(N)`` makes it worse: it sleeps wall-clock, then still
calls ``_wait_for_screen`` after.

These helpers wrap ``pilot.pause()`` so timeouts are swallowed and
state changes are awaited via predicate polling on a wall-clock
budget — robust to load spikes that exceed the 30 s internal bound.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from textual.pilot import Pilot, WaitForScreenTimeout

from tests._failure_state import describe


async def safe_pause(pilot: Pilot[None]) -> None:
    """``pilot.pause()`` that swallows ``WaitForScreenTimeout``.

    Use at test setup points where draining is desired but a load
    spike must not crash the test. Falls back to a short idle ladder
    if the underlying pause times out.
    """
    try:
        await pilot.pause()
    except WaitForScreenTimeout:
        for _ in range(8):
            await asyncio.sleep(0)


async def safe_press(pilot: Pilot[None], *keys: str) -> None:
    """``pilot.press(*keys)`` that swallows ``WaitForScreenTimeout``.

    ``pilot.press`` posts keys via ``_app._press_keys`` then calls
    ``_wait_for_screen`` directly — that 30 s timeout is the same load
    flake source we work around in ``safe_pause``.
    """
    try:
        await pilot.press(*keys)
    except WaitForScreenTimeout:
        # Keys were posted; just yield so the app drains them.
        for _ in range(8):
            await asyncio.sleep(0)


async def settle(pilot: Pilot[None], ticks: int = 4) -> None:
    """Repeated ``safe_pause`` to drain the message loop. ``ticks``
    matches the legacy ``for _ in range(n): await pilot.pause()``
    shape — each iteration tolerates a ``WaitForScreenTimeout``."""
    for _ in range(max(1, ticks)):
        await safe_pause(pilot)


# Wall clock the identical samples must span. Three rounds of a degraded
# ``safe_pause`` elapse in tens of microseconds, over which nothing can have
# moved. Covered by continuing to pump, not by sleeping.
_STABLE_MIN_SPAN = 0.05


async def wait_stable(
    pilot: Pilot[None],
    sample: Callable[[], object],
    *,
    rounds: int = 3,
    timeout: float = 10.0,
    message: str = "",
) -> None:
    """Wait until ``sample()`` is unchanged across ``rounds`` consecutive rounds
    spanning at least ``_STABLE_MIN_SPAN`` seconds.

    The event-gated replacement for ``settle(pilot, ticks=N)``: a tick count
    assumes each ``safe_pause`` flushes a refresh, but a load spike degrades it
    to ``asyncio.sleep(0)``, so exactly when the wait is needed it does least.
    Sampling the thing you actually care about — a scroll offset, a virtual
    size — is load-proof, and the wall-clock budget bounds it.

    Prefer gating on a product signal (``is_restoring``, ``pipeline_busy()``)
    where one exists; use this when the only evidence is geometry holding
    still.
    """
    started = time.monotonic()
    deadline = started + timeout
    unreadable = object()
    last: object = object()
    stable = 0
    stable_since = started
    rounds_run = 0
    while True:
        try:
            current = sample()
        except Exception:
            # Mid-rebuild the widget may not be queryable. That is "still
            # moving", not a test failure — keep waiting.
            current = unreadable
        if current == last and current is not unreadable:
            stable += 1
        else:
            stable = 0
            stable_since = time.monotonic()
        last = current
        rounds_run += 1
        if stable >= rounds and time.monotonic() - stable_since >= _STABLE_MIN_SPAN:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"wait_stable gave up after {time.monotonic() - started:.1f}s "
                f"(budget {timeout}s, {rounds_run} rounds): "
                f"{message or 'sample never held still'} (last={current!r})\n"
                f"{describe(pilot)}"
            )
        await safe_pause(pilot)


async def wait_until(
    pilot: Pilot[None],
    predicate: Callable[[], bool | Awaitable[bool]],
    *,
    timeout: float = 10.0,
    poll: float = 0.02,
    message: str = "",
) -> None:
    """Poll ``predicate`` until truthy or ``timeout`` (wall-clock) elapses.

    Each iteration: evaluate predicate, then yield the event loop via
    ``safe_pause`` (idle drain) or a small sleep so background tasks
    and timers can advance.
    """
    started = time.monotonic()
    deadline = started + timeout
    iters = 0
    last_error: Exception | None = None
    while True:
        try:
            result = predicate()
            if asyncio.iscoroutine(result):
                result = await result  # type: ignore[assignment]
            if result:
                return
        except Exception as exc:
            # A predicate that raises every round reads identically to one that
            # is merely False, and the two want opposite fixes.
            last_error = exc
        iters += 1
        if time.monotonic() >= deadline:
            raised = f" (predicate last raised {last_error!r})" if last_error is not None else ""
            raise AssertionError(
                f"wait_until gave up after {time.monotonic() - started:.1f}s "
                f"(budget {timeout}s, {iters} polls): "
                f"{message or 'predicate stayed False'}{raised}\n"
                f"{describe(pilot)}"
            )
        # Alternate idle drains and short sleeps. ``safe_pause`` flushes
        # call_later queues (when load allows); ``sleep(poll)`` keeps
        # progress when the drain itself times out.
        if iters % 2 == 1:
            await safe_pause(pilot)
        else:
            await asyncio.sleep(poll)


async def run_search(pilot: Pilot[None], app: Any, query: str, *, timeout: float = 10.0) -> None:
    """Issue a query and wait for it to land.

    ``SearchController.run`` returns as soon as the worker is dispatched, so
    the old ``run(q)`` + one ``pilot.pause()`` under-waits. Gate on the
    controller's own ``idle`` signal — a product signal, not a tick count.

    ``timeout`` is exposed because several call sites budget longer than the
    default for search readiness (15 s, 30 s); without it they would gate on a
    fixed 10 s window before their own longer waits even began.
    """
    app._search.run(query)
    await wait_until(
        pilot,
        lambda: app._search.idle,
        timeout=timeout,
        message=f"search for {query!r} never committed",
    )

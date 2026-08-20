"""Search runs off the event loop.

The point is a UI that stays alive while a query runs — but the change
has to be safe before it is useful, so most of what is pinned here is
the staleness and teardown behaviour rather than the responsiveness.

The property everything rests on: Textual *cancels* a superseded thread
worker but cannot *interrupt* it. The old search runs to completion and
arrives anyway. So the generation guard in ``_commit`` is not a
belt-and-braces extra — it is the only thing standing between a stale
result and the caches.
"""

from __future__ import annotations

import textwrap
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from fnd.index import build_index
from fnd.tui import FNDApp
from tests._pilot_wait import run_search, wait_until


@pytest.fixture
def index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    root = tmp_path / "docs"
    root.mkdir()
    (root / "alpha.md").write_text("# Alpha\n\nalpha target one\n", encoding="utf-8")
    (root / "beta.md").write_text(
        textwrap.dedent("""
        # Beta

        beta target two with some more words in it.
        """),
        encoding="utf-8",
    )
    build_index(roots=[root], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_a_query_returns_to_the_caller_immediately(index: Path) -> None:
    """``run()`` dispatches and returns; it no longer blocks the loop for the
    duration of the search."""
    app = FNDApp(index_dir=index)
    async with app.run_test() as pilot:
        await pilot.pause()
        started = threading.Event()
        release = threading.Event()
        original = app._search._execute

        def slow(request: Any) -> Any:
            started.set()
            release.wait(timeout=10.0)
            return original(request)

        app._search._execute = slow  # type: ignore[method-assign]

        app._search.run("target")
        # The loop is still ours: the worker has not even been entered yet, and
        # crucially run() came back rather than sitting inside the search.
        assert not app._search.idle
        await wait_until(pilot, started.is_set, message="worker never started")
        # Still responsive while the search is parked in its thread.
        assert app.query_one("#query_bar") is not None
        release.set()
        await wait_until(pilot, lambda: app._search.idle)
        assert app._search.groups


@pytest.mark.asyncio
async def test_the_progress_line_is_up_while_the_search_runs(index: Path) -> None:
    app = FNDApp(index_dir=index)
    async with app.run_test() as pilot:
        await pilot.pause()
        started = threading.Event()
        release = threading.Event()
        original = app._search._execute

        def slow(request: Any) -> Any:
            started.set()
            release.wait(timeout=10.0)
            return original(request)

        app._search._execute = slow  # type: ignore[method-assign]
        app._search.run("target")
        await wait_until(pilot, started.is_set)
        session = app._progress.active
        assert session is not None, "a running search shows nothing"
        assert session.operation_id == "search"
        release.set()
        await wait_until(pilot, lambda: app._search.idle)


@pytest.mark.asyncio
async def test_a_superseded_search_never_reaches_the_caches(index: Path) -> None:
    """The staleness guard. A slow first query is overtaken by a second; when
    the first finally finishes it must be discarded whole — not merged, not
    partially applied."""
    app = FNDApp(index_dir=index)
    async with app.run_test() as pilot:
        await pilot.pause()
        release = threading.Event()
        started = threading.Event()
        original = app._search._execute

        def slow_first(request: Any) -> Any:
            if request.query == "alpha":
                started.set()
                release.wait(timeout=10.0)
            return original(request)

        app._search._execute = slow_first  # type: ignore[method-assign]

        app._search.run("alpha")
        await wait_until(pilot, started.is_set)
        app._search.run("beta")  # supersedes while "alpha" is parked
        await wait_until(pilot, lambda: app._search.current_query == "beta")

        committed = list(app._search.groups)
        trace = app._search.latest_trace

        release.set()  # let the stale search finish and try to commit
        for _ in range(8):
            await pilot.pause()

        # Assert the stale commit had NO effect, rather than asserting
        # something the winning query satisfies on its own: the earlier
        # `all(...) or any("beta" ...)` was true the moment "beta" landed, so
        # it would have passed even if "alpha" had merged into the caches.
        assert app._search.current_query == "beta", (
            "a superseded search overwrote the current query"
        )
        assert [g.path for g in app._search.groups] == [g.path for g in committed], (
            "a superseded search reached the result set"
        )
        assert app._search.latest_trace is trace, "a superseded search reached the explain trace"
        assert all("alpha.md" not in g.path for g in app._search.groups), (
            "the stale query's file is in the committed results"
        )


@pytest.mark.asyncio
async def test_a_malformed_query_still_fails_on_the_spot(index: Path) -> None:
    """Validation stays on the loop: the notice must not wait for a thread."""
    app = FNDApp(index_dir=index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._search.run("{60}")
        notice = app.query_one("#query_notice")
        assert notice.display is True
        assert app._search.idle, "a rejected query left the controller busy"


@pytest.mark.asyncio
async def test_an_exploding_search_does_not_strand_the_line(index: Path) -> None:
    """An unexpected error inside the search must land as a notice, not as a
    progress line that never clears."""
    app = FNDApp(index_dir=index)
    async with app.run_test() as pilot:
        await pilot.pause()

        def boom(_request: Any) -> Any:
            raise RuntimeError("kaboom")

        app._search._execute = boom  # type: ignore[method-assign]
        app._search.run("target")
        await wait_until(
            pilot,
            lambda: app._search.idle,
            message="a failed search left the controller busy forever",
        )
        await wait_until(pilot, lambda: app._progress.active is None)


@pytest.mark.asyncio
async def test_quitting_mid_search_is_not_an_error(index: Path) -> None:
    """A search in flight when the app goes away has no DOM to commit to.
    That is an ordinary shutdown, not a crashed worker — which is exactly
    what it surfaced as before ``_marshal`` guarded it."""
    app = FNDApp(index_dir=index)
    async with app.run_test() as pilot:
        await pilot.pause()
        started = threading.Event()
        original = app._search._execute

        def slow(request: Any) -> Any:
            started.set()
            time.sleep(0.3)
            return original(request)

        app._search._execute = slow  # type: ignore[method-assign]
        app._search.run("target")
        await wait_until(pilot, started.is_set)
    # Leaving the block tears the app down with the search still running; the
    # test failing here would mean the worker raised.


@pytest.mark.asyncio
async def test_the_initial_query_lands_without_a_synchronous_read(index: Path) -> None:
    """``on_mount`` used to read ``groups`` straight after ``run()`` to decide
    focus. It cannot any more, so the results tree has to claim focus itself
    once they arrive."""
    app = FNDApp(index_dir=index, initial_query="target")
    async with app.run_test() as pilot:
        await pilot.pause()
        await wait_until(pilot, lambda: bool(app._search.groups))
        await wait_until(
            pilot,
            lambda: app.query_one("#results_pane").has_focus,
            message="results never took focus after the initial query",
        )


@pytest.mark.asyncio
async def test_idle_tracks_every_issued_query(index: Path) -> None:
    app = FNDApp(index_dir=index)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._search.idle
        await run_search(pilot, app, "target")
        assert app._search.idle


@pytest.mark.asyncio
async def test_a_malformed_query_supersedes_the_search_already_running(index: Path) -> None:
    """The generation has to be claimed before the parse can fail.

    ``_fail`` marks the current generation committed. Allocating the
    generation only after a successful parse meant a malformed query marked an
    IN-FLIGHT search's generation as committed — that worker then passed the
    guard in ``_commit``, restored its stale results, and wiped the error
    notice the user had just been shown.
    """
    app = FNDApp(index_dir=index)
    async with app.run_test() as pilot:
        await pilot.pause()
        started = threading.Event()
        release = threading.Event()
        original = app._search._execute

        def slow(request: Any) -> Any:
            started.set()
            release.wait(timeout=10.0)
            return original(request)

        app._search._execute = slow  # type: ignore[method-assign]
        app._search.run("target")
        await wait_until(pilot, started.is_set)

        app._search.run("{60}")  # malformed, rejected on the loop
        notice = app.query_one("#query_notice")
        assert notice.display is True

        release.set()  # the superseded search finishes and tries to commit
        for _ in range(8):
            await pilot.pause()

        assert notice.display is True, (
            "the superseded search committed and cleared the error notice"
        )
        assert not app._search.groups, "a superseded search reached the result set"


@pytest.mark.asyncio
async def test_the_search_plan_uses_both_of_its_phases(index: Path) -> None:
    """A declared phase that is never entered is dead weight: it caps the line
    at the earlier phase's share and its duration never reaches calibration,
    so its weight stays at the seed for good."""
    from fnd.tui.progress.operations import SEARCH

    app = FNDApp(index_dir=index)
    async with app.run_test() as pilot:
        await pilot.pause()
        phases: list[str] = []
        original_enter = app._search.__class__._commit

        def recording(self: Any, request: Any, groups: Any, trace: Any, session: Any) -> Any:
            result = original_enter(self, request, groups, trace, session)
            phases.append(session.phase)
            return result

        app._search.__class__._commit = recording  # type: ignore[method-assign]
        try:
            await run_search(pilot, app, "target")
        finally:
            app._search.__class__._commit = original_enter  # type: ignore[method-assign]

    assert phases, "setup — the search never committed"
    assert phases[-1] == "results", "the commit stage never entered its own phase"
    assert {p.key for p in SEARCH.phases} == {"query", "results"}


@pytest.mark.asyncio
async def test_a_malformed_query_does_not_reload_the_index_under_a_worker(
    index: Path,
) -> None:
    """``reload()`` reassigns the searcher's inner snapshot, and a running
    search reads it more than once, so reloading under one could serve a
    single search from two index generations.

    The guard used to derive "nothing is running" from the generation
    counters — but ``_fail`` marks a generation committed WITHOUT waiting for
    its worker, so a malformed query in between reported idle while a real
    search was still executing.
    """
    app = FNDApp(index_dir=index)
    async with app.run_test() as pilot:
        await pilot.pause()
        started = threading.Event()
        release = threading.Event()
        original = app._search._execute
        reloads: list[int] = []

        def slow(request: Any) -> Any:
            started.set()
            release.wait(timeout=10.0)
            return original(request)

        assert app._search.searcher is not None
        original_reload = app._search.searcher.reload

        def counting_reload(*a: Any, **k: Any) -> Any:
            reloads.append(1)
            return original_reload(*a, **k)

        app._search._execute = slow  # type: ignore[method-assign]
        app._search.searcher.reload = counting_reload  # type: ignore[method-assign]

        app._search.run("target")
        await wait_until(pilot, started.is_set)
        # The first query reloads legitimately — nothing was running. What
        # follows is the case under test.
        reloads.clear()

        app._search.run("{60}")  # malformed: rejected on the loop, marks committed
        await pilot.pause()
        app._search.run("second")  # would reload if the worker looked finished

        assert not reloads, "the index was reloaded while a search was still running"
        release.set()
        await wait_until(pilot, lambda: app._search.idle)

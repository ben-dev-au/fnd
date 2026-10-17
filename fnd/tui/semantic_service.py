"""Semantic search in the TUI: the query model, the current vectors, and the embedding job.

The query model and vectors load off the loop once the app is idle after launch, so the first
search does not pay for them. :meth:`SemanticService.context` is what a search captures; it is
None unless semantic search is installed, switched on and has vectors. The embedding job runs
in its own process (:mod:`fnd.embed_job`), pauses while the user searches or an index runs,
and shows on the progress line unless ``show_embedding_progress`` is off.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final

from fnd.tui.progress.model import OperationKind, OperationPlan, Phase

if TYPE_CHECKING:
    from fnd.embed_job import EmbedJobRunner, TimeLeft
    from fnd.semantic import Embedder
    from fnd.semantic_search import SemanticContext
    from fnd.tui.app import FNDApp
    from fnd.tui.progress.facility import ProgressSession

__all__ = ["EMBED", "SemanticService"]

# Seeded from the owner's library: 109,345 sections in 30 minutes on an M1 Max.
EMBED: Final = OperationPlan(
    operation_id="semantic.embed",
    phases=(Phase(key="sections", expected_ms=1_800_000.0, countable=True),),
    kind=OperationKind.AMBIENT,
)
_PAUSE_POLL_S: Final = 0.5
# Progress lines between reloads of the vectors a search uses: about a minute.
_REFRESH_EVERY: Final = 200
# Seconds before retrying a job that found another writer holding the store.
_BUSY_RETRY_S: Final = 60.0


def _ready() -> bool:
    from fnd.semantic import is_ready

    return is_ready()


class SemanticService:
    def __init__(
        self,
        app: FNDApp,
        *,
        ready: Callable[[], bool] = _ready,
        embedder_factory: Callable[[], Embedder] | None = None,
        runner_factory: Callable[[], EmbedJobRunner] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._app = app
        self._ready = ready
        self._embedder_factory = embedder_factory
        self._runner_factory = runner_factory
        self._embedder: Embedder | None = None
        self._ctx: SemanticContext | None = None
        self._runner: EmbedJobRunner | None = None
        self._job: asyncio.Task[None] | None = None
        self._session: ProgressSession | None = None
        self._pause_timer: Any = None
        self._reconciled_for: object = None
        self._clock = clock
        self._time_left: TimeLeft | None = None

    # ── state ────────────────────────────────────────────────────

    def _defaults(self) -> Any:
        config = getattr(self._app, "_config", None)
        return config.defaults if config is not None else None

    @property
    def enabled(self) -> bool:
        d = self._defaults()
        return bool(d is not None and d.semantic_search)

    def opted_in(self) -> frozenset[str]:
        config = getattr(self._app, "_config", None)
        if config is None:
            return frozenset()
        return frozenset(name for name, c in config.collections.items() if c.semantic)

    def context(self) -> SemanticContext | None:
        """What a search blends with, or None to search on words alone."""
        ctx = self._ctx
        if ctx is None or not self.enabled:
            return None
        searcher = getattr(getattr(self._app, "_search", None), "searcher", None)
        generation = getattr(searcher, "generation", None)
        moved = searcher is not None and generation != ctx.rows.stamp
        # Once per generation: a stamp that can never match must not respawn the job per search.
        if moved and not self.job_running and generation != self._reconciled_for:
            self._reconciled_for = generation
            self._app.call_later(self.ensure_job)
        opted = self.opted_in()
        return ctx if ctx.opted_in == opted else _with_opted(ctx, opted)

    def use_context(self, ctx: SemanticContext | None) -> None:
        self._ctx = ctx

    def release(self) -> None:
        """Let go of the model and the mapped vectors, closing their files."""
        import gc

        self._ctx = None
        self._embedder = None
        gc.collect()

    @property
    def job_running(self) -> bool:
        return self._runner is not None and self._runner.running

    # ── loading ──────────────────────────────────────────────────

    def on_launch(self) -> None:
        """Load the model and vectors once the first frame is up, then reconcile."""
        if not self.enabled or not self._ready():
            return
        self._app.call_after_refresh(self._load_in_background)

    def _load_in_background(self) -> None:
        self._app.run_worker(self._load, thread=True, group="semantic_load", exit_on_error=False)

    def _load(self) -> None:
        self._load_context()
        self._app.call_from_thread(self.ensure_job)

    def _load_context(self) -> None:
        from fnd.semantic import load_embedder
        from fnd.semantic_search import load_context

        if self._embedder is None:
            from fnd.semantic_search import QUERY_THREADS

            factory = self._embedder_factory or (lambda: load_embedder(threads=QUERY_THREADS))
            self._embedder = factory()
        ctx = load_context(self._embedder)
        if ctx is not None:
            self._ctx = ctx

    def settings_changed(self) -> None:
        """Semantic search was switched, installed, removed or a collection opted in or out."""
        if not self.enabled or not self._ready():
            self._app.run_worker(self.stop(), group="semantic_stop", exit_on_error=False)
            return
        if self._embedder is None:
            self._load_in_background()
        elif self.job_running:
            self._app.run_worker(self._restart(), group="semantic_restart", exit_on_error=False)
        else:
            self.ensure_job()

    async def _restart(self) -> None:
        """A running job embeds the collections it started with; restart it for the new set."""
        await self.stop()
        self.ensure_job()

    def after_index(self) -> None:
        """An index run finished: bring the vectors in line with it."""
        if self.enabled and self._ready():
            self.ensure_job()

    # ── the job ──────────────────────────────────────────────────

    def ensure_job(self) -> None:
        if self.job_running or not self.enabled or not self._ready() or not self.opted_in():
            return
        if self._indexing():
            return
        self._job = asyncio.get_running_loop().create_task(self._run_job())

    def _indexing(self) -> bool:
        task = getattr(getattr(self._app, "_indexer", None), "task", None)
        return task is not None and not task.done()

    def _make_runner(self) -> EmbedJobRunner:
        if self._runner_factory is not None:
            return self._runner_factory()
        from fnd.embed_job import EmbedJobRunner
        from fnd.semantic import GRANITE
        from fnd.vectors import store_dir

        return EmbedJobRunner(index_dir=self._app._index_dir, store_dir=store_dir(GRANITE))  # type: ignore[attr-defined]

    async def _run_job(self) -> None:
        self._runner = runner = self._make_runner()
        await runner.start(self.opted_in())
        self._pause_timer = self._app.set_interval(_PAUSE_POLL_S, self._update_pause)
        seen = 0
        try:
            async for p in runner.progress():
                self._show(p.done, p.total)
                seen += 1
                if seen % _REFRESH_EVERY == 0:
                    await asyncio.to_thread(self._load_context)
        finally:
            if self._pause_timer is not None:
                self._pause_timer.stop()
                self._pause_timer = None
            self._close_session()
        await asyncio.to_thread(self._load_context)
        from fnd.embed_job import BUSY_EXIT

        if runner.exit_code == BUSY_EXIT:
            self._app.set_timer(_BUSY_RETRY_S, self.ensure_job)

    def _update_pause(self) -> None:
        runner = self._runner
        if runner is None or not runner.running:
            return
        progress = getattr(self._app, "_progress", None)
        active = getattr(progress, "_active", None)
        busy = (active is not None and not active.closed) or self._indexing()
        runner.pause(busy)

    def _show(self, done: int, total: int) -> None:
        d = self._defaults()
        if d is None or not d.show_embedding_progress or total <= 0 or self._indexing():
            self._close_session()
            return
        if self._session is None or self._session.closed:
            from fnd.embed_job import TimeLeft

            self._session = self._app._progress.begin(EMBED)  # type: ignore[attr-defined]
            self._time_left = TimeLeft(self._clock) if self._clock else TimeLeft()
        self._session.enter("sections")
        self._session.report(done, total)
        left = self._time_left.text(done, total) if self._time_left else ""
        self._session.set_label(f"Embedding {done:,} / {total:,}" + (f" · {left}" if left else ""))

    def _close_session(self) -> None:
        """Full only for a run that finished; a stopped one is abandoned, not claimed done."""
        session, self._session = self._session, None
        latest = self._runner.latest if self._runner is not None else None
        if session is None or session.closed:
            return
        if latest is not None and latest.done >= latest.total:
            session.close()
        else:
            session.abandon()

    async def stop(self) -> None:
        """Stop the job and wait for its process to exit."""
        runner = self._runner
        if runner is not None:
            await runner.stop()
        job = self._job
        if job is not None and not job.done():
            job.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await job

    def stop_now(self) -> None:
        """At quit: tell the job to stop at its next batch, without waiting."""
        runner = self._runner
        if runner is not None:
            runner.terminate()


def _with_opted(ctx: SemanticContext, opted: frozenset[str]) -> SemanticContext:
    import dataclasses

    return dataclasses.replace(ctx, opted_in=opted)

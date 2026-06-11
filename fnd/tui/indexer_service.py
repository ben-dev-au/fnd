"""Background indexer lifecycle for the TUI.

``IndexerService`` owns the async reindex task, its cancellation and
event plumbing, and the update-all chain bookkeeping. The modal
(``fnd/tui/indexer_modal.py``) drives and reads this state through the
app's accessors; chain continuations re-enter via ``app.start_indexer``
so test-level patches on the app class keep intercepting starts.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fnd.query import Searcher
from fnd.tui.indexer_modal import IndexerScreen, drive_indexer

if TYPE_CHECKING:
    from fnd.tui.app import FNDApp

__all__ = ["IndexerService"]


class IndexerService:
    """Owns the indexer task/cancel/event state and the reindex entry
    points; one instance lives on the app for the session."""

    def __init__(self, app: FNDApp) -> None:
        self._app = app
        # Async indexer state — see fnd/tui/indexer_modal.py. None when
        # no indexer is running; populated for the lifetime of one
        # `fnd collection reindex` invoked from the TUI / palette.
        self.task: asyncio.Task[None] | None = None
        self.cancel: asyncio.Event | None = None
        self.events: asyncio.Queue[Any] | None = None
        self.state: Any = None
        self.last_event: Any = None
        # Run generation. Each new explicit run/chain bumps it; a chain
        # continuation inherits it. A run's teardown only touches the
        # shared chain state when it's still the current generation, so a
        # cancelled run winding down LATE can't clobber the chain a newer
        # run just set up. Also gates the serialise-on-restart path.
        self.run_seq: int = 0
        # Holds the "await the in-flight run, then start mine" coroutine so
        # it isn't garbage-collected before it runs.
        self.deferred_task: asyncio.Task[None] | None = None
        # Update-all-collections chain bookkeeping. The IndexerScreen
        # title shows "(N of M)" when total > 1; drive_indexer in
        # indexer_modal.py dequeues from _indexer_chain_remaining at
        # the end of each collection's run.
        self.chain_remaining: list[str] = []
        self.chain_total: int = 1
        # True between drive_indexer scheduling the next chain step via
        # call_later and the deferred task actually starting. The
        # IndexerScreen drain reads this so the modal stays mounted
        # across that gap. Without the guard the drain pops the modal
        # as soon as _indexer_chain_remaining empties (which happens
        # synchronously before call_later fires) and the next
        # collection's events have no consumer.
        self.chain_callback_pending: bool = False
        # Per-run texturise override carried across chain steps. None
        # means follow the "Texturise PDFs while indexing" toggle; True
        # forces texturising on (set by the shared "Update everything"
        # action); False forces it off (set by the "Process new files
        # index-only" action). Reset to None when the chain finishes.
        self.texturise_override: bool | None = None
        # Run-mode flags for the indexer chain, stashed so each chain step
        # inherits them. skip_unchanged=False + force_fresh=True is the
        # "Re-texturise outdated" action; defaults give incremental indexing
        # with durable cache reuse.
        self.skip_unchanged: bool = True
        self.force_fresh: bool = False
        # rebuild=True drops each collection's chunks before re-indexing —
        # the "Rebuild" actions pair it with force_fresh for a true redo.
        self.rebuild: bool = False
        # Per-collection final snapshots captured as each chain step
        # finishes. Drives the IndexerScreen's history band and the
        # post-chain summary screen so the user can see what every
        # finished collection produced - even after the modal moved
        # on. Cleared by the Done action on IndexerScreen.
        self.chain_history: list[Any] = []
        self.collection: str = ""
        self.started_at: str = ""

    def start(
        self,
        *,
        collection: str,
        config: Any = None,  # CollectionConfig; Any to avoid import cycle
        index_dir: Path | None = None,
        rebuild: bool = False,
        open_modal: bool = True,
        texturise_override: bool | None = None,
        skip_unchanged: bool = True,
        force_fresh: bool = False,
        _bump_seq: bool = True,
    ) -> bool:
        """Spawn the async indexer task for ``collection``.

        ``texturise_override`` (None/True/False), ``skip_unchanged`` and
        ``force_fresh`` are forwarded through ``drive_indexer`` to
        ``run_indexer``; for chain runs they are stashed so subsequent
        chain steps inherit the same mode.

        A new explicit request (``_bump_seq=True``) bumps the run
        generation. If a run is already in flight, it is cancelled and
        this one is started only once the old has fully torn down — so the
        old teardown can never race the new setup (and can't clobber this
        request's chain queue). Chain continuations pass ``_bump_seq=False``
        to inherit the current generation.

        Returns True when a new task was spawned, False when deferred.
        """
        import datetime as _dt

        from fnd.config import load as _load_config

        if self.task is not None and not self.task.done():
            cancelling = self.cancel is not None and self.cancel.is_set()
            if not (_bump_seq and cancelling):
                # Either a chain continuation racing a busy task (defensive)
                # or an actively-running run the user re-opened to watch:
                # don't start a second, don't disturb its generation. Show
                # the running modal so "view progress" still works.
                if open_modal and _bump_seq:
                    self._app.push_screen(IndexerScreen(self.collection or collection))
                return False
            # In flight but already cancelling (cancel-then-start-again):
            # serialise — bump the generation so the dying run's teardown
            # knows it's superseded and won't clobber this request's chain,
            # then start fresh once it has fully torn down.
            self.run_seq += 1
            my_seq = self.run_seq
            with contextlib.suppress(Exception):
                from fnd.extract._worker import request_cancel

                request_cancel()
            with contextlib.suppress(Exception):
                self._app.notify("Finishing the cancelled run before starting…", timeout=3)
            old_task = self.task

            async def _await_then_start() -> None:
                with contextlib.suppress(Exception):
                    await old_task
                # Only proceed if no newer request superseded this one.
                if self.run_seq != my_seq:
                    return
                self._app.start_indexer(
                    collection=collection,
                    config=config,
                    index_dir=index_dir,
                    rebuild=rebuild,
                    open_modal=open_modal,
                    texturise_override=texturise_override,
                    skip_unchanged=skip_unchanged,
                    force_fresh=force_fresh,
                    _bump_seq=False,
                )

            self.deferred_task = asyncio.create_task(_await_then_start())
            return False

        if _bump_seq:
            self.run_seq += 1
        my_seq = self.run_seq
        if config is None:
            cfg = _load_config()
            config = cfg.collection(collection)
        if index_dir is None:
            # Use the app's configured index_dir so a test or CLI
            # caller that constructed FNDApp(index_dir=...) actually
            # writes to that directory. ``self._app._index_dir`` is set
            # from the constructor (falling back to ``default_index_dir()``
            # there) and is always non-None.
            index_dir = self._app._index_dir
        self.collection = collection
        self.started_at = _dt.datetime.now(tz=_dt.UTC).isoformat(timespec="seconds")
        # First step of a chain (or single-collection run) sets the
        # mode; later chain steps re-enter start_indexer via
        # _start_next_in_chain, which passes the stashed values back.
        self.texturise_override = texturise_override
        self.skip_unchanged = skip_unchanged
        self.force_fresh = force_fresh
        self.rebuild = rebuild
        self.cancel = asyncio.Event()
        # Reuse the existing events queue when a chain run is in
        # progress so the IndexerScreen's drain (which holds a
        # reference to the queue from its on_mount) keeps seeing
        # events from the next collection. Otherwise the modal would
        # appear to stall after the first collection completes.
        chain_active = bool(self.chain_remaining) or (self.chain_total or 1) > 1
        if not chain_active or self.events is None:
            self.events = asyncio.Queue()
        if not chain_active:
            # Fresh chain start: clear session-wide per-page counters
            # so this run reports its own avg from scratch.
            with contextlib.suppress(Exception):
                from fnd.tui.live_progress import reset_session as _live_reset_session

                _live_reset_session()
        self.state = None
        self.last_event = None
        self.task = asyncio.create_task(
            drive_indexer(
                self._app,
                collection=collection,
                config=config,
                index_dir=index_dir,
                rebuild=rebuild,
                cancel=self.cancel,
                events=self.events,
                texturise_override=texturise_override,
                skip_unchanged=skip_unchanged,
                force_fresh=force_fresh,
                run_seq=my_seq,
            )
        )
        if open_modal:
            chain_total = getattr(self, "chain_total", 1) or 1
            chain_pending = getattr(self, "chain_remaining", None) or []
            chain_index = max(1, chain_total - len(chain_pending))
            self._app.push_screen(
                IndexerScreen(
                    collection,
                    chain_total=chain_total,
                    chain_index=chain_index,
                )
            )
        return True

    def reindex_with_warning(
        self,
        collection: str,
        *,
        texturise_override: bool | None = None,
        skip_unchanged: bool = True,
        force_fresh: bool = False,
        rebuild: bool = False,
    ) -> None:
        """If the pdf-structure extra is installed and the first-reindex
        warning hasn't been seen, show it; on confirm, start the
        indexer. Otherwise start the indexer directly.

        ``rebuild=True`` is for callers that need a fresh build after a
        config change (collection rename, source delete, etc.) — it
        drops the collection's existing chunks before re-indexing."""
        from fnd.config import load as _load_config
        from fnd.tui.first_reindex_warning import (
            FirstReindexWarningScreen,
            count_pdfs,
            has_been_seen,
        )

        # Prefer the in-memory config so tests / live edits don't get
        # silently overridden by whatever's on disk.
        cfg = self._app._config if self._app._config is not None else _load_config()
        if collection not in cfg.collections:
            self._app.notify(f"Collection '{collection}' not found.", severity="error")
            return
        col_cfg = cfg.collections[collection]

        # Only warn when extras are actually installed (otherwise the
        # cost is the old flat-extraction cost, which is sub-second/PDF).
        from fnd.extras import EXTRAS, is_extra_installed

        extra = EXTRAS.get("pdf-structure")
        show_warning = extra is not None and is_extra_installed(extra) and not has_been_seen()

        if show_warning:
            n_pdfs = count_pdfs(col_cfg)
            if n_pdfs == 0:
                # No PDFs in this collection; skip the warning entirely.
                self._app.start_indexer(
                    collection=collection,
                    config=col_cfg,
                    rebuild=rebuild,
                    texturise_override=texturise_override,
                    skip_unchanged=skip_unchanged,
                    force_fresh=force_fresh,
                )
                return

            def _after_warning(confirmed: bool | None) -> None:
                if confirmed:
                    self._app.start_indexer(
                        collection=collection,
                        config=col_cfg,
                        rebuild=rebuild,
                        texturise_override=texturise_override,
                        skip_unchanged=skip_unchanged,
                        force_fresh=force_fresh,
                    )

            self._app.push_screen(
                FirstReindexWarningScreen(collection=collection, n_pdfs=n_pdfs),
                _after_warning,
            )
        else:
            self._app.start_indexer(
                collection=collection,
                config=col_cfg,
                rebuild=rebuild,
                texturise_override=texturise_override,
                skip_unchanged=skip_unchanged,
                force_fresh=force_fresh,
            )

    def maybe_resume(self) -> None:
        """Auto-resume an interrupted background index on launch — only when
        the user has opted in (defaults.indexer_auto_resume, off by default)
        AND the saved state is genuinely resumable. Indexing is heavy, so it
        must never start unbidden; see index_runner.is_state_resumable for the
        recent-and-real-collection guard that rejects leaked/stale state."""
        import datetime as _dt

        from fnd.config import default_index_dir
        from fnd.index_runner import is_state_resumable, load_state, state_file_for

        try:
            from fnd.config import load as _load_config

            cfg = _load_config()
        except Exception:
            return
        if not cfg.defaults.indexer_auto_resume:
            return
        # Resume the default collection only for now — extending to named
        # collections requires walking the reindex dir for *.state.toml.
        state = load_state(state_file_for("default"))
        if not is_state_resumable(
            state, known_collections=set(cfg.collections), now=_dt.datetime.now(tz=_dt.UTC)
        ):
            return
        assert state is not None  # narrowed by is_state_resumable
        try:
            self._app.start_indexer(
                collection="default", index_dir=default_index_dir(), open_modal=False
            )
            self._app.notify(
                f"Resuming indexing: {state.files_completed}/{state.total_files}",
                timeout=5,
            )
        except Exception:
            pass

    def reindex_collection_async(self, name: str) -> None:
        """Worker that drops + rebuilds chunks for ``name``. Notifies on
        start/finish/error. Reused by SourceFormScreen, RenameCollection,
        and the Reindex action in the per-collection sub-menu."""
        # Reload config so we hit the latest source list.
        import contextlib

        from fnd.config import load
        from fnd.index import build_index_from_config

        with contextlib.suppress(Exception):
            self._app._config = load()
        cfg = self._app._config
        if cfg is None or name not in cfg.collections:
            return
        col = cfg.collections[name]
        index_dir = self._app._index_dir

        def _run() -> None:
            self._app.call_from_thread(
                self._app.notify,
                f"Reindexing {name}…",
                severity="information",
                timeout=3,
            )
            try:
                n = build_index_from_config(
                    config=col, collection=name, index_dir=index_dir, rebuild=True
                )
            except Exception as e:
                self._app.call_from_thread(
                    self._app.notify, f"Reindex failed: {e}", severity="error"
                )
                return
            self._app.call_from_thread(self._app._indexer.on_reindex_complete)
            self._app.call_from_thread(
                self._app.notify,
                f"Indexed {n} chunks for {name}.",
                severity="information",
            )

        self._app.run_worker(_run, thread=True, exclusive=True, group=f"reindex-{name}")

    def on_reindex_complete(self) -> None:
        """Swap the in-memory ``Searcher`` for a fresh one after a rebuild.

        The captured ``self._index.searcher()`` inside ``Searcher`` reads
        from the index generation it was opened against; once the writer
        commits new chunks, the old searcher still returns hits from the
        previous generation. Rebuilding the ``Searcher`` is cheap (just
        reopens the directory) and the in-flight ``_chunk_cache`` is
        invalidated by ``_run_query`` immediately below so callers don't
        see ghost rows from the old gen.
        """
        try:
            self._app._search.searcher = Searcher(index_dir=self._app._index_dir)
        except (FileNotFoundError, RuntimeError, ValueError):
            self._app._search.searcher = None
        if self._app._search.current_query:
            self._app._search.run(self._app._search.current_query)

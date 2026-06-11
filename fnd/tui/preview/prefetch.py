"""Background prefetch warming for the preview pane.

``PrefetchEngine`` decodes and pre-mounts an N-result window around the
cursor so file switches land on cache hits. Widget mounts run through a
single-consumer sink queue; the user-side mount always preempts.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from textual.containers import VerticalScroll

from fnd.tui.line_buffer import build_rendered_document
from fnd.tui.preview import tuning
from fnd.tui.preview_dispatcher import choose_preview_mode
from fnd.tui.widgets.markdown import FNDMarkdown
from fnd.tui.widgets.preview_container import PreviewContainer

if TYPE_CHECKING:
    from fnd.query import FileChunk
    from fnd.tui.app import FNDApp
    from fnd.tui.line_buffer import RenderedDocument

__all__ = ["PrefetchEngine"]


class PrefetchEngine:
    """Owns the prefetch sink queue/drainer and the top-N warming
    pipeline; one instance lives on the app for the session."""

    def __init__(self, app: FNDApp) -> None:
        self._app = app
        # Single-consumer drainer serializes prefetch widget-mounts.
        self.sink_queue: asyncio.Queue[Any] | None = None
        self.sink_drainer: Any | None = None

    def start(self) -> None:
        """Create the sink queue and spawn the drainer task. Called from
        the app's ``on_mount`` so task timing matches the app lifecycle."""
        self.sink_queue = asyncio.Queue()
        self.sink_drainer = asyncio.create_task(self.drain_sinks())

    async def cancel_task_on(self, container: PreviewContainer) -> None:
        """Cancel + await any background prefetch task on ``container``
        so the user-side mount doesn't race it and trip MountError."""
        import asyncio
        import contextlib

        task = getattr(container, "_prefetch_task", None)
        if task is None:
            return
        try:
            if task.done():
                container._prefetch_task = None  # type: ignore[attr-defined]
                return
        except Exception:
            container._prefetch_task = None  # type: ignore[attr-defined]
            return
        task.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
        container._prefetch_task = None  # type: ignore[attr-defined]

    def prefetch_top_results(self, *, anchor_parent_id: str | None = None) -> None:
        """Decode + pre-mount widgets for an N-result window so cursor moves
        land on cache hits. ``preview_prefetch_count`` = N; 0 disables.
        Parallelism bounded by ``preview_decode_workers``.

        ``anchor_parent_id`` centres the window around the cursor's position
        in the result list instead of starting from the top — lets the
        buffer follow the user when they navigate past the initial range.
        """
        # Discard mount jobs queued for the previous anchor — stale
        # work would otherwise keep the drainer (and the asyncio loop)
        # busy across navigation.
        q = self.sink_queue
        if q is not None:
            import contextlib as _contextlib

            drained = 0
            while True:
                try:
                    q.get_nowait()
                except Exception:
                    break
                with _contextlib.suppress(Exception):
                    q.task_done()
                drained += 1
            if drained:
                self._app._diag_log(f"prefetch_top drained_stale_jobs={drained}")
        if self._app._search.searcher is None or not self._app._search.groups:
            return
        if self._app._config is not None:
            n = self._app._config.defaults.preview_prefetch_count
        else:
            from fnd.config import Defaults

            n = Defaults().preview_prefetch_count
        if n <= 0:
            return
        # Build the candidate window. With no anchor we walk from rank 0;
        # with an anchor we start ~N/2 above its position so we cover both
        # directions of likely navigation.
        start_idx = 0
        if anchor_parent_id is not None:
            anchor_idx = next(
                (
                    i
                    for i, g in enumerate(self._app._search.groups)
                    if g.parent_id == anchor_parent_id
                ),
                -1,
            )
            if anchor_idx >= 0:
                half = max(1, n // 2)
                start_idx = max(0, anchor_idx - half)
        targets: list[tuple[str, int]] = []
        seen: set[str] = set()
        already_cached: list[str] = []
        query_sig_for_filter = self._app._search.query_signature()
        for g in self._app._search.groups[start_idx:]:
            if g.parent_id in seen:
                continue
            seen.add(g.parent_id)
            # Filter by preview_cache (widget tree ready), not chunk_cache:
            # a file whose chunks are cached but whose mount got drained
            # by a prior cursor move must be re-queued. Also skip if it's
            # the active preview — that one's owned by the user-side path.
            in_preview = (
                self._app._preview.preview_cache.get(g.parent_id, query_sig_for_filter) is not None
            )
            is_active = (
                self._app._preview.active is not None
                and self._app._preview.active.parent_doc_id == g.parent_id
                and self._app._preview.active.query_signature == query_sig_for_filter
            )
            if in_preview or is_active:
                already_cached.append(g.parent_id[:8])
                continue
            focus = g.hits[0].chunk_seq if g.hits else 0
            targets.append((g.parent_id, focus))
            if len(targets) >= n:
                break
        self._app._diag_log(
            f"prefetch_top n={n} anchor={anchor_parent_id[:8] if anchor_parent_id else None} "
            f"start_idx={start_idx} targets={[t[0][:8] for t in targets]} "
            f"already_cached={already_cached}"
        )
        if not targets:
            return

        searcher = self._app._search.searcher
        decode_workers = (
            self._app._config.defaults.preview_decode_workers
            if self._app._config is not None
            else 1
        )
        try:
            pane_widget = self._app.query_one("#preview_pane", VerticalScroll)
            # Floor of 20 mirrors the cold-load + md-flat paths. Without
            # it, if the pane isn't laid out at prefetch time (content
            # width 0–1) every flat-path file gets pre-rendered to
            # 1-cell strips and paints as a single vertical column on
            # first reveal — the "PDF only shows a single line" symptom.
            measured = pane_widget.content_size.width - 1
            estimated_wrap_width = max(20, measured) if measured > 0 else 0
        except Exception:
            estimated_wrap_width = 0
        query_sig = self._app._search.query_signature()
        app = self._app

        def _prefetch_one(parent_id: str, focus_seq: int) -> None:
            import time as _time

            t0 = _time.perf_counter()
            # Reuse cached chunk data if present — only the mount got dropped,
            # not the decode. Avoids re-running PDF/docx extraction.
            cached_chunks = app._preview.chunk_cache.get(parent_id)
            if cached_chunks is not None:
                fetched = cached_chunks
                decode_ms = 0.0
            else:
                try:
                    fetched = searcher.get_file_chunks(parent_id, max_workers=decode_workers)
                except Exception:
                    app.call_from_thread(
                        app._diag_log,
                        f"prefetch_one decode FAILED parent={parent_id[:8]}",
                    )
                    return
                decode_ms = (_time.perf_counter() - t0) * 1000.0
            # Stale-query guard: if the user has moved on, drop the
            # work without scheduling any main-thread sinks.
            if query_sig != app._search.query_signature():
                app.call_from_thread(
                    app._diag_log,
                    f"prefetch_one stale parent={parent_id[:8]} decode_ms={decode_ms:.0f}",
                )
                return
            app.call_from_thread(app._prefetch.record_chunks, parent_id, fetched)
            if not fetched:
                return
            mode = choose_preview_mode(fetched)
            app.call_from_thread(
                app._diag_log,
                f"prefetch_one done parent={parent_id[:8]} decode_ms={decode_ms:.0f} "
                f"chunks={len(fetched)} mode={mode} focus_seq={focus_seq}",
            )
            if mode == "flat":
                try:
                    fv = app._flat.build_file_view(fetched)
                    wrap_width = estimated_wrap_width if estimated_wrap_width > 0 else 0
                    doc = build_rendered_document(fv, wrap_width=wrap_width)
                except Exception:
                    return
                app.call_from_thread(app._prefetch.record_bundle, parent_id, query_sig, doc)
                app.call_from_thread(app._prefetch.mount_flat, parent_id, query_sig, doc, focus_seq)
            else:
                app.call_from_thread(
                    app._prefetch.mount_structural,
                    parent_id,
                    query_sig,
                    list(fetched),
                    focus_seq,
                )

        def _prefetch() -> None:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            workers = max(1, decode_workers)
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = [ex.submit(_prefetch_one, pid, focus) for pid, focus in targets]
                for f in as_completed(futures):
                    # Drop everything on query change — _run_query has cleared
                    # caches and any in-flight work here is stale.
                    if query_sig != app._search.query_signature():
                        for other in futures:
                            other.cancel()
                        return
                    with contextlib.suppress(Exception):
                        f.result()

        self._app.run_worker(
            _prefetch,
            thread=True,
            exclusive=True,
            group="preview-prefetch",
            description="prefetching top-N preview bundles",
        )

    def record_chunks(self, parent_id: str, chunks: list[FileChunk]) -> None:
        """Main-thread sink for prefetch worker chunk results. Stored
        only if not already present so a concurrent user-initiated
        load (which would have richer state) wins."""
        if parent_id not in self._app._preview.chunk_cache:
            self._app._preview.chunk_cache[parent_id] = chunks

    def record_bundle(
        self,
        parent_id: str,
        query_sig: str,
        doc: RenderedDocument,
    ) -> None:
        """Stash a worker-built bundle if the query is still current."""
        if query_sig != self._app._search.query_signature():
            return
        self._app._preview.prebuilt_cache[(parent_id, query_sig)] = doc

    def mount_flat(
        self,
        parent_id: str,
        query_sig: str,
        doc: RenderedDocument,
        focus_chunk_seq: int,
    ) -> None:
        """Queue a hidden flat-buffer pre-mount; drainer runs it serially."""
        q = self.sink_queue
        if q is None:
            return
        if query_sig != self._app._search.query_signature():
            return

        async def _job() -> None:
            await self._mount_flat_async(parent_id, query_sig, doc, focus_chunk_seq)

        q.put_nowait(_job)

    async def _mount_flat_async(
        self,
        parent_id: str,
        query_sig: str,
        doc: RenderedDocument,
        focus_chunk_seq: int,
    ) -> None:
        """Stash the prefetched RenderedDocument in the value cache. No mount —
        user activation installs into the shared widget on click."""
        _ = focus_chunk_seq  # focus is recomputed at install time
        if query_sig != self._app._search.query_signature():
            return
        cache_key = (parent_id, query_sig)
        if cache_key in self._app._flat.cache:
            return
        self._app._flat.cache[cache_key] = doc
        self._app._flat.cache.move_to_end(cache_key)
        while len(self._app._flat.cache) > tuning.PREVIEW_CACHE_MAX_FILES:
            self._app._flat.cache.popitem(last=False)

    def mount_structural(
        self,
        parent_id: str,
        query_sig: str,
        chunks: list[FileChunk],
        focus_chunk_seq: int,
    ) -> None:
        """Queue a hidden structural pre-mount so cached clicks land
        as a visibility flip. Safe to default-on now that W3 collapses
        per-cell widgets — see bench_input_lag for the DOM-size
        breakdown. Opt out with _FND_NO_PREMOUNT=1."""
        import os as _os

        if _os.environ.get("_FND_NO_PREMOUNT") == "1":
            return
        q = self.sink_queue
        if q is None:
            self._app._diag_log(
                f"prefetch_mount_structural SKIPPED no-queue parent={parent_id[:8]}"
            )
            return
        if query_sig != self._app._search.query_signature():
            self._app._diag_log(
                f"prefetch_mount_structural SKIPPED stale-sig parent={parent_id[:8]}"
            )
            return
        self._app._diag_log(
            f"prefetch_mount_structural QUEUED parent={parent_id[:8]} "
            f"focus={focus_chunk_seq} qsize_before={q.qsize()}"
        )

        async def _job() -> None:
            await self._mount_structural_async(parent_id, query_sig, chunks, focus_chunk_seq)

        q.put_nowait(_job)

    async def _mount_structural_async(
        self,
        parent_id: str,
        query_sig: str,
        chunks: list[FileChunk],
        focus_chunk_seq: int,
    ) -> None:
        if query_sig != self._app._search.query_signature():
            self._app._diag_log(
                f"prefetch_mount_structural_async SKIPPED stale-sig parent={parent_id[:8]}"
            )
            return
        if self._app._preview.preview_cache.get(parent_id, query_sig) is not None:
            self._app._diag_log(
                f"prefetch_mount_structural_async SKIPPED already-cached parent={parent_id[:8]}"
            )
            return
        if (
            self._app._preview.active is not None
            and self._app._preview.active.parent_doc_id == parent_id
            and self._app._preview.active.query_signature == query_sig
        ):
            self._app._diag_log(
                f"prefetch_mount_structural_async SKIPPED already-active parent={parent_id[:8]}"
            )
            return
        import asyncio
        import contextlib

        try:
            pane = self._app.query_one("#preview_pane", VerticalScroll)
        except Exception:
            self._app._diag_log(
                f"prefetch_mount_structural_async SKIPPED no-pane parent={parent_id[:8]}"
            )
            return
        self._app._diag_log(f"prefetch_mount_structural_async STARTING parent={parent_id[:8]}")
        container = PreviewContainer(
            parent_doc_id=parent_id,
            query_signature=query_sig,
            total_chunks=len(chunks),
        )
        container.add_class("-hidden")
        mount_awaitable: object | None = None
        with contextlib.suppress(Exception):
            mount_awaitable = pane.mount(container)
        if mount_awaitable is not None:
            with contextlib.suppress(Exception):
                await mount_awaitable  # type: ignore[misc]

        sub_task = asyncio.create_task(
            self._mount_chunk_loop(parent_id, query_sig, focus_chunk_seq, chunks, container)
        )
        # Exposing the sub-task as _prefetch_task lets the user-side adopt
        # branch (_cancel_prefetch_task_on) await its cancellation cleanly.
        container._prefetch_task = sub_task  # type: ignore[attr-defined]
        try:
            await sub_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _mount_chunk_loop(
        self,
        parent_id: str,
        query_sig: str,
        focus_chunk_seq: int,
        chunks: list[FileChunk],
        container: PreviewContainer,
    ) -> None:
        import asyncio
        import contextlib

        from fnd.tui import _perf

        focus_idx = next(
            (i for i, c in enumerate(chunks) if c.chunk_seq == focus_chunk_seq),
            0,
        )
        # Prefetch only mounts a tiny window around the focused chunk
        # so the DOM stays small across many cached files. User-side
        # resume expands on click via Phase 1b/2.
        win_start = max(0, focus_idx - tuning.PREFETCH_MOUNT_RADIUS)
        win_end = min(len(chunks), focus_idx + tuning.PREFETCH_MOUNT_RADIUS + 1)
        _perf.mark(
            "prefetch_loop_start",
            parent_id=parent_id,
            focus_idx=focus_idx,
            win=(win_start, win_end),
            total_chunks=len(chunks),
        )
        self._app._diag_log(
            f"prefetch_loop_start parent={parent_id[:8]} focus={focus_idx} "
            f"win=({win_start},{win_end}) total_chunks={len(chunks)}"
        )
        n_mounted = 0
        try:
            for i in range(win_start, win_end):
                if query_sig != self._app._search.query_signature():
                    return
                if i in container.mounted_indices:
                    continue
                # Bail out the moment user-side mount lights up: prefetch is
                # background warming, foreground always wins.
                if self._app._preview.user_mount_in_flight():
                    return
                try:
                    with _perf.span("prefetch_mount_one", idx=i):
                        self._app._preview.mount_chunk_into(container, chunks[i], i, chunks)
                    n_mounted += 1
                except Exception:
                    continue
                # Wait for the chunk widget's async build so
                # ``first_match_block`` resolves before a user-side
                # click adopts this pre-mount; without this, the click
                # path's retry chain polls ~500 ms for a still-running build.
                seq = chunks[i].chunk_seq
                md_widget = container.chunk_widgets.get(seq)
                if md_widget is not None and isinstance(md_widget, FNDMarkdown):
                    with contextlib.suppress(Exception), _perf.span("prefetch_await_build", idx=i):
                        async with md_widget.lock:
                            pass
                await asyncio.sleep(0.002)
        finally:
            _perf.mark(
                "prefetch_loop_end",
                parent_id=parent_id,
                n_mounted=n_mounted,
                mounted_indices_size=len(container.mounted_indices),
                is_complete=container.is_complete,
            )
            self._app._diag_log(
                f"prefetch_loop_end parent={parent_id[:8]} n_mounted={n_mounted} "
                f"mounted_size={len(container.mounted_indices)} "
                f"is_complete={container.is_complete}"
            )
            if container.mounted_indices:
                evicted = self._app._preview.preview_cache.put(
                    container, protect=self._app._preview.active
                )
                for old in evicted:
                    with contextlib.suppress(Exception):
                        old.remove()
            else:
                # Loop bailed on user-mount-in-flight (or every mount raised)
                # before any chunk landed. Caching the empty container would
                # block the next prefetch attempt for this (parent_id, sig)
                # via the already-cached short-circuit; instead, drop it so a
                # later trigger (cursor move, second query) can retry cleanly.
                with contextlib.suppress(Exception):
                    container.remove()

    async def drain_sinks(self) -> None:
        """Single-consumer drainer. Runs prefetch widget-mount jobs one at a
        time and yields to user-side mount before each."""
        import asyncio
        import contextlib

        q = self.sink_queue
        assert q is not None
        while True:
            job = await q.get()
            self._app._diag_log(f"drainer JOB pulled qsize={q.qsize()}")
            wait_iters = 0
            # Cooperative wait — user-side mount always preempts.
            while self._app._preview.user_mount_in_flight():
                wait_iters += 1
                await asyncio.sleep(0.05)
            if wait_iters > 0:
                self._app._diag_log(f"drainer JOB started after {wait_iters * 50}ms wait")
            try:
                await job()
            except Exception as e:
                self._app._diag_log(f"drainer JOB threw: {type(e).__name__}: {e}")
            with contextlib.suppress(Exception):
                q.task_done()
            await asyncio.sleep(0)

"""Structural preview core: load scheduling, mount, reveal, settle.

``PreviewPresenter`` owns the chunk/widget caches, the active and
outgoing containers, the debounced load pipeline, the visible-first
mount with background fill, and the scroll-settle / atomic-reveal
machinery. Scroll *positioning* stays with PreviewScrollController on
the app; this class feeds it and reacts to its callbacks.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from textual.containers import Container, VerticalScroll
from textual.widget import Widget
from textual.widgets import Static, Tree

from fnd.matching import MatchSpec
from fnd.render import render_chunk_pieces
from fnd.tui.line_buffer import LineBufferPreview, build_rendered_document
from fnd.tui.preview import tuning
from fnd.tui.preview_dispatcher import choose_preview_mode, uses_markdown_renderer
from fnd.tui.preview_scroll import ScrollAnchor
from fnd.tui.preview_scrollbar import MatchAwareScroll
from fnd.tui.widgets.markdown import FNDMarkdown, _legacy_blocks_to_md
from fnd.tui.widgets.preview_container import PreviewCache, PreviewContainer

if TYPE_CHECKING:
    from fnd.query import FileChunk, FileGroup, Hit
    from fnd.tui.app import FNDApp
    from fnd.tui.line_buffer import RenderedDocument

__all__ = ["PreviewPresenter"]


class PreviewPresenter:
    """Owns structural-preview state and the load → mount → reveal
    pipeline; one instance lives on the app for the session."""

    def __init__(self, app: FNDApp) -> None:
        self._app = app
        # Set around the scroll controller's own structural scroll so the
        # resulting scroll-watcher trip isn't mistaken for a user scroll
        # and doesn't self-release the anchor.
        self.reconciling: bool = False
        # Cache of (parent_id) → list[FileChunk] so we don't re-fetch the
        # full document on every cursor move within the same file. Keyed by
        # parent_id, invalidated on new query.
        self.chunk_cache: dict[str, list[FileChunk]] = {}
        # Widget-level cache (UX-pass-4 §4 hybrid): keeps the mounted
        # widget tree alive across file switches so repeat visits are
        # O(1). Cleared on every new query (highlights would be wrong).
        self.preview_cache: PreviewCache = PreviewCache()
        # The currently-active PreviewContainer (the one with `-active`
        # class). None until the first file is rendered.
        self.active: PreviewContainer | None = None
        # The previously-visible container, kept on screen during a cold/swap
        # mount so the pane never blanks: the incoming container builds
        # invisibly (opacity:0) and only when its scroll lands do we hide this
        # one and reveal the new one in a single tick. Cleared by that swap.
        self.outgoing: PreviewContainer | None = None
        # Convenience aliases that point into the active container —
        # legacy code paths (_scroll_preview_to_chunk, etc.) read from
        # these instead of poking at the container directly.
        # Widgets here may be either per-line ``Static``s (PDF / TXT
        # plain renderer) or whole-chunk ``FNDMarkdown`` widgets (md
        # / docx / pptx structural renderer). The dict is widened to
        # ``Widget`` so both can be stored without complaint.
        self.chunk_widgets: dict[int, Widget] = {}
        self.match_targets: dict[int, Widget] = {}
        # The parent_id whose chunks are currently mounted in the preview
        # pane (so we don't re-mount when cursor moves within the same file).
        self.parent_id: str | None = None
        # (loaded, total) while a chunk-decode + mount worker is running.
        self.load_progress: tuple[int, int | None] | None = None
        # Strong ref so the event loop doesn't GC the in-flight mount task.
        self.mount_task: object | None = None
        # Prebuilt flat-buffer bundles keyed by (parent_id, query_sig).
        # Cleared on query change — highlight spans are baked in at build time.
        self.prebuilt_cache: dict[tuple[str, str], RenderedDocument] = {}
        # Debounced preview load — latest target + Timer.
        from typing import Any as _Any

        self.load_timer: _Any | None = None
        self.load_target: tuple[str, int] | None = None
        # The (parent_id, focus_chunk_seq) of the render currently in
        # flight, so redundant identical dispatches landing in the same
        # tick coalesce. Cleared when that render finishes settling.
        self.inflight_target: tuple[str, int] | None = None
        # Monotonic generation for the off-thread scrollbar-marker scan: each
        # refresh bumps it so a worker that finishes after a newer file/query
        # superseded it drops its now-stale result instead of overwriting.
        self._markers_seq: int = 0
        # Bumped on every full preview reset (new query / scope clear / highlight
        # rerender). A mount task captures it at start; if it has moved by the
        # time the task's `finally` runs, that reset cleared the caches + DOM
        # under it — so the task must NOT resurrect its now-stale container
        # (re-cache it / leave it mounted). cancel_mount_task only *requests*
        # cancellation; the finally runs a tick later and would otherwise race
        # the reset and re-pollute it ("stuck mid-mount after a new query").
        self.reset_generation: int = 0

    def schedule_load(self, parent_id: str, focus_chunk_seq: int) -> None:
        """Debounce a cursor-move → preview-load; coalesces rapid arrow sweeps."""
        # Preempt stale tail-mount on the previous file so the loop is
        # free during the debounce window.
        active_parent = self.active.parent_doc_id if self.active is not None else None
        if active_parent is not None and active_parent != parent_id:
            self.cancel_mount_task()
            self._app._lazy.cancel()
            # The cancelled mount will never reach settle to clear the in-flight
            # coalescing latch. If that latched target differs from where the
            # cursor is now heading, drop it — otherwise returning to it later
            # (an overshoot-and-correct sweep) hits the dedup guard as "already
            # in flight" and the remount is suppressed, stranding the preview
            # mid-mount until an unrelated nav resets the latch.
            if self.inflight_target is not None and self.inflight_target != (
                parent_id,
                focus_chunk_seq,
            ):
                self.inflight_target = None
        self.load_target = (parent_id, focus_chunk_seq)
        if self._app._config is not None:
            delay_ms = self._app._config.defaults.preview_load_debounce_ms
        else:
            from fnd.config import Defaults

            delay_ms = Defaults().preview_load_debounce_ms
        if delay_ms <= 0:
            self.fire_pending_load()
            return
        if self.load_timer is not None:
            with contextlib.suppress(Exception):
                self.load_timer.stop()
        self.load_timer = self._app.set_timer(
            delay_ms / 1000.0,
            self.fire_pending_load,
            name="preview-load-debounce",
        )

    def fire_pending_load(self) -> None:
        self.load_timer = None
        target = self.load_target
        if target is None:
            return
        self.load_target = None
        parent_id, focus_chunk_seq = target
        # Coalesce redundant identical loads. A query both parks the
        # cursor (which fires NodeHighlighted) AND dispatches explicitly
        # as a fallback for when the cursor index is unchanged, so the
        # same (parent, seq) load can land several times in one tick.
        # With the debounce pinned to 0 these don't merge; the 2nd+ then
        # warm-resume and cancel the 1st's still-building mount, orphaning
        # the focus chunk's build_done and losing the match scroll. If the
        # exact same render is already in flight, skip — it will land it.
        if self.inflight_target == (parent_id, focus_chunk_seq):
            return
        self.inflight_target = (parent_id, focus_chunk_seq)
        # Re-anchor prefetch around where the cursor actually settled
        # every time, not only on cache miss. Cursor-following: window
        # follows the user instead of waiting for them to outrun it.
        # Prefetch is an exclusive-group worker so the previous run is
        # cancelled cleanly.
        self._app._prefetch.prefetch_top_results(anchor_parent_id=parent_id)
        self.render_full_doc(parent_id, focus_chunk_seq=focus_chunk_seq)

    def cancel_pending_load(self) -> None:
        if self.load_timer is not None:
            with contextlib.suppress(Exception):
                self.load_timer.stop()
            self.load_timer = None
        self.load_target = None

    def render_full_doc(self, parent_id: str, *, focus_chunk_seq: int) -> None:
        """Render the full document for ``parent_id`` as one widget per
        chunk, then scroll to the chunk identified by ``focus_chunk_seq``.

        Hybrid load (UX-pass-4 §4 follow-up):

        1. Look up the cached :class:`PreviewContainer` for this file +
           query. If complete, activate (O(1) class flip) and scroll —
           done.
        2. If partial (resume case), activate, scroll, dispatch a task
           that mounts only the un-mounted indices.
        3. If absent and the chunk DATA is cached, create a fresh
           container and dispatch the visible-first + background mount.
        4. If even the chunk data is missing, dispatch the decode worker
           first; its callback enters step 3.
        """
        import asyncio

        from fnd.tui import _perf

        _perf.mark("click_to_display_start", parent_id=parent_id, focus_seq=focus_chunk_seq)

        # Any pending debounce timer is now moot — we're committing to
        # a load. Cancel so a late-firing timer can't race the current
        # dispatch and clobber it with a stale target.
        self.cancel_pending_load()

        if self._app._search.searcher is None:
            return

        # Arm the scroll controller for this navigation. Every mount/finalize
        # event below reconciles against this one anchor instead of issuing its
        # own scroll, so call order can no longer change where the preview lands.
        # Glide smoothly only when the target match is ALREADY mounted (the
        # content between is on screen, so the scroll is over real rows). A
        # fresh file — or a same-file match outside the mounted window — is
        # revealed via an atomic swap (cut) instead: animating over an unmounted
        # gap would be lumpy, and prepending an out-of-window window above the
        # current match slides the view (reflow). Consistent rule: glide when
        # the content is there, cut when it must be built.
        active = self.active
        target_mounted = (
            active is not None
            and active.parent_doc_id == parent_id
            and (active.is_complete or focus_chunk_seq in active.chunk_widgets)
        )
        self._app._preview_scroll.arm(
            ScrollAnchor(parent_id, focus_chunk_seq, animate=target_mounted)
        )

        chunks = self.chunk_cache.get(parent_id)
        if chunks is not None:
            # We have decoded data — go to the mount path. If the
            # prefetch worker (or an earlier load) has already built
            # the flat-path bundle for this (file, query) pair, pass
            # it through so the dispatcher skips the main-thread
            # FileView + strip rebuild entirely.
            query_sig = self._app._search.query_signature()
            prebuilt = self.prebuilt_cache.get((parent_id, query_sig))
            self.dispatch_mount(parent_id, focus_chunk_seq, chunks, prebuilt=prebuilt)
            return

        # Need to decode first. The bar appears immediately; the worker
        # decodes off-thread and its callback re-enters via the chunk
        # data path.
        self.cancel_mount_task()
        # Keep the previously-active content visible during the decode
        # rather than blanking the pane. The app-level progress strip
        # is the user-visible loading signal, and the debounced cursor-
        # move dispatch means the user has already committed to this
        # file before we land here. The flat-buffer
        # ``_activate_flat_buffer`` / structural ``_activate_preview_container``
        # paths swap visibility atomically once the new content is ready.
        self.show_progress_bar(total=1, phase="decoding…")

        target_parent_id = parent_id
        target_focus = focus_chunk_seq
        searcher = self._app._search.searcher
        # Pull the worker count from config so users can tune the
        # decode parallelism via Settings without code edits. 1 = serial.
        decode_workers = (
            self._app._config.defaults.preview_decode_workers
            if self._app._config is not None
            else 1
        )
        # Estimate the wrap width the eventual ``LineBufferPreview``
        # will be laid out at, so the worker can pre-render Strips at
        # the correct width and avoid a main-thread rewrap on first
        # paint. ``content_size`` excludes the pane's padding; the
        # buffer itself reserves one extra column for its own
        # ``scrollbar-gutter: stable``. If the estimate ends up wrong
        # (e.g. the user resizes the terminal between dispatch and
        # paint) ``_rebuild_after_layout`` will catch it.
        try:
            pane_widget = self._app.query_one("#preview_pane", VerticalScroll)
            # Floor of 20 — see prefetch _prefetch_top_results for the
            # PDF-single-column rationale.
            measured = pane_widget.content_size.width - 1
            estimated_wrap_width = max(20, measured) if measured > 0 else 0
        except Exception:
            estimated_wrap_width = 0
        app = self._app

        def _load() -> None:
            try:
                fetched = searcher.get_file_chunks(target_parent_id, max_workers=decode_workers)
            except Exception as e:
                app.call_from_thread(app._preview.on_load_failed, e)
                return
            # For the flat-buffer path (PDF / TXT) the FileView build —
            # which computes per-chunk match spans and stitches the
            # global line buffer — and the per-line ``rich.Console.render``
            # pass are pure-Python data work that easily dominate the
            # main-thread cost on large documents. Do both here in the
            # worker so the UI stays responsive during the decode +
            # assemble phase. Structural formats (md / docx / pptx)
            # skip this path; their mount path is per-chunk.
            prebuilt: RenderedDocument | None = None
            try:
                if fetched and choose_preview_mode(fetched) == "flat":
                    fv = app._flat.build_file_view(fetched)
                    wrap_width = estimated_wrap_width if estimated_wrap_width > 0 else 0
                    prebuilt = build_rendered_document(fv, wrap_width=wrap_width)
            except Exception:
                # Best-effort; fall back to main-thread build inside the dispatcher.
                prebuilt = None
            app.call_from_thread(
                app._preview.on_chunks_loaded,
                target_parent_id,
                target_focus,
                fetched,
                prebuilt,
            )

        _ = asyncio.get_event_loop()  # ensure a loop exists for the callback
        self._app.run_worker(_load, thread=True, exclusive=True, group="preview-load")

    def prune_active_to_window(self, margin: int = 3) -> None:
        """Drop the currently-active container's off-screen chunks down to its
        visible window. Used when switching files: the outgoing container stays
        on screen while the incoming one builds, so its full-mounted DOM would
        otherwise inflate the incoming mount's arrange (Option C's inter-file
        cost). Flash-free — the visible window stays put; chunks removed ABOVE
        the viewport are scroll-compensated so the on-screen content doesn't
        shift while the outgoing container is still visible during the swap."""
        import contextlib

        container = self.active
        if container is None:
            return
        window = tuning.VISIBLE_FIRST_ABOVE + tuning.VISIBLE_FIRST_BELOW + 2 * margin + 1
        if len(container.mounted_indices) <= window:
            return  # not enough off-screen DOM to be worth pruning
        try:
            pane = self._app.query_one("#preview_pane", VerticalScroll)
        except Exception:
            return
        if pane.size.height <= 0:
            return
        chunks = self.chunk_cache.get(container.parent_doc_id)
        if not chunks:
            return
        vtop = float(pane.scroll_y)
        vbot = vtop + float(pane.size.height)
        ranges: list[tuple[int, Widget, float, float]] = []
        for i in sorted(container.mounted_indices):
            if i >= len(chunks):
                # Unreachable today: mounted_indices is built from THIS
                # _chunk_cache list, each file decodes once per query, and a
                # query change clears the chunk AND preview caches together — so
                # a cached container always matches its chunks. Cheap crash-guard
                # only, in case that coupling is ever broken; there is no live
                # stale state here to normalise.
                continue
            seq = chunks[i].chunk_seq
            w = container.chunk_widgets.get(seq)
            if w is None:
                continue
            try:
                vr = w.virtual_region  # type: ignore[attr-defined]
                ranges.append((i, w, float(vr.y), float(vr.y + vr.height)))
            except Exception:
                return  # geometry not ready — skip rather than risk a bad scroll
        visible = [i for (i, _w, y0, y1) in ranges if y1 > vtop and y0 < vbot]
        if not visible:
            return
        keep_lo, keep_hi = min(visible) - margin, max(visible) + margin
        above_height = 0.0
        to_remove: list[tuple[int, Widget]] = []
        for i, w, y0, y1 in ranges:
            if i < keep_lo:
                above_height += y1 - y0
                to_remove.append((i, w))
            elif i > keep_hi:
                to_remove.append((i, w))
        if not to_remove:
            return
        import time as _time

        from fnd.tui import _perf

        _pt0 = _time.perf_counter()
        self.begin_reconcile_scroll()
        try:
            for i, w in to_remove:
                seq = chunks[i].chunk_seq
                # display:none leaves the arrange immediately; remove() then frees
                # it. (Keeping ~1000s of display:none widgets alive is worse — they
                # still get walked by settle and inflate the next mount.)
                with contextlib.suppress(Exception):
                    w.display = False
                with contextlib.suppress(Exception):
                    w.remove()
                container.mounted_indices.discard(i)
                container.chunk_widgets.pop(seq, None)
                container.match_targets.pop(seq, None)
            if above_height > 0:
                with contextlib.suppress(Exception):
                    pane.scroll_to(y=max(0.0, vtop - above_height), animate=False, immediate=True)
        finally:
            self.end_reconcile_scroll()
        _perf.mark("prune", removed=len(to_remove), ms=(_time.perf_counter() - _pt0) * 1000.0)

    def dispatch_mount(
        self,
        parent_id: str,
        focus_chunk_seq: int,
        chunks: list[FileChunk],
        *,
        prebuilt: RenderedDocument | None = None,
    ) -> None:
        """Route flat vs structural. ``prebuilt`` is a worker-built bundle for
        flat path; structural ignores it."""
        import asyncio

        # Phase 5 redesign: route by format. PDF / TXT take the flat-
        # buffer path (one widget per file, line API, line-precise
        # scrollbar markers). MD / DOCX / PPTX stay on the structural
        # Markdown widget below.
        if choose_preview_mode(chunks) == "flat":
            self.dispatch_flat_mount(parent_id, focus_chunk_seq, chunks, prebuilt=prebuilt)
            return

        query_sig = self._app._search.query_signature()

        # Same file + same query already active. Two sub-cases:
        #   (a) target chunk widget exists — just scroll, no remount.
        #   (b) target chunk not yet mounted (still loading the file
        #       and the user clicked a result above the load front):
        #       cancel the in-flight task and resume the SAME container
        #       with the new focus window. We keep all already-mounted
        #       chunks; the worker only mounts the missing ones.
        if (
            self.active is not None
            and self.active.parent_doc_id == parent_id
            and self.active.query_signature == query_sig
        ):
            container = self.active
            if container.is_complete or focus_chunk_seq in container.chunk_widgets:
                # Target already mounted (Option C full-mount makes this the
                # common case for internal jumps). A bare reconcile() scrolls
                # before heavy match geometry is final and lands off-screen, so
                # route through the scoped settle (cheap when already idle).
                import asyncio as _asyncio

                self.mount_task = _asyncio.create_task(
                    self._settled_instant_scroll(container, parent_id, focus_chunk_seq)
                )
                return
            # Same-file, target match OUTSIDE the mounted window. Resuming the
            # SAME container would mount the new window in document order —
            # which, for an upward jump, prepends above the current match and
            # slides the visible content (the "flash wrong content, then land"
            # reflow). Instead build a FRESH container at the new focus and
            # atomic-swap to it, exactly like a between-file nav: the fresh
            # container builds invisibly (mounted below the current one, so no
            # shift), then the swap hides the old and reveals the new at the
            # match in one tick. The old container is dropped from the cache by
            # the fresh one's put() and swept on the next navigation.
            self.cancel_mount_task()
            self._app._lazy.cancel()
            self.hide_progress_bar()
            fresh = PreviewContainer(
                parent_doc_id=parent_id,
                query_signature=query_sig,
                total_chunks=len(chunks),
            )
            self.mount_task = asyncio.create_task(
                self._mount_chunks_async(
                    parent_id,
                    focus_chunk_seq,
                    chunks,
                    fresh,
                    reset_generation=self.reset_generation,
                )
            )
            return

        self.cancel_mount_task()
        # Option C hardening: the outgoing file may be FULL-mounted (~1000s of
        # widgets). It stays on screen while the incoming file builds, and
        # Textual's arrange scales with total DOM — so a big outgoing container
        # inflates the new file's mount several-fold. Prune it to its visible
        # window now (flash-free) so the incoming mount is cheap.
        self.prune_active_to_window()
        # Sweep stranded containers — but preserve any still being filled by
        # a prefetch task. Removing those would orphan the task and trigger
        # a MountError on its next mount-before call.
        import contextlib as _contextlib

        cached_containers = set(self.preview_cache._cache.values())
        for stranded in list(self._app.query(PreviewContainer)):
            if stranded in cached_containers:
                continue
            pfetch = getattr(stranded, "_prefetch_task", None)
            if pfetch is not None and not pfetch.done():
                continue
            with _contextlib.suppress(Exception):
                stranded.remove()
        if self.active is not None and self.active not in cached_containers:
            self.active = None
        # Adopt a still-in-flight prefetched container for this key so the
        # cold branch resumes it; _mount_chunks_async cancels its prefetch
        # task first to avoid concurrent-mount races.
        cached = self.preview_cache.get(parent_id, query_sig)
        if cached is None:
            for c in self._app.query(PreviewContainer):
                if (
                    c.parent_doc_id == parent_id
                    and c.query_signature == query_sig
                    and c not in cached_containers
                ):
                    cached = c
                    break

        import os

        reveal_first = os.environ.get("_FND_REVEAL_FIRST") == "1"
        cache_keys = [f"{pid[:8]}/{sig[:6]}" for (pid, sig) in self.preview_cache._cache]
        dom_keys = [
            f"{c.parent_doc_id[:8]}/{c.query_signature[:6]}"
            f"(t={'a' if getattr(c, '_prefetch_task', None) is not None and not c._prefetch_task.done() else 'd'})"  # pyright: ignore[reportAttributeAccessIssue]
            for c in self._app.query(PreviewContainer)
        ]
        self._app._diag_log(
            f"dispatch_preview cache_check parent={parent_id[:8]} sig={query_sig[:6]} "
            f"cached={'yes' if cached is not None else 'no'} "
            f"is_complete={cached.is_complete if cached is not None else None} "
            f"focus_in_widgets={focus_chunk_seq in cached.chunk_widgets if cached is not None else False} "
            f"focus_seq={focus_chunk_seq} reveal_first_env={reveal_first} "
            f"cache_keys={cache_keys} dom_keys={dom_keys}"
        )
        if cached is not None and (
            cached.is_complete or (reveal_first and focus_chunk_seq in cached.chunk_widgets)
        ):
            # Reveal-first: activate visible, scroll on next refresh.
            if reveal_first:
                self.activate_container(cached, pre_reveal=False)
                self.refresh_match_scrollbar(chunks)
                # One-tick scroll: _do_scroll_to_chunk's own retry chain
                # handles any residual region.height==0 race. The prior
                # two-tick wrapping was wasting a refresh tick (~50-200ms
                # depending on DOM size) for every cache-hit click.
                self._app.call_after_refresh(self._app._preview_scroll.reconcile)
                if not cached.is_complete:
                    # Resume the partial mount in the background; the
                    # scroll above is canonical so suppress the task's
                    # own scroll attempts.
                    import asyncio as _asyncio

                    self.mount_task = _asyncio.create_task(
                        self._mount_chunks_async(
                            parent_id,
                            focus_chunk_seq,
                            chunks,
                            cached,
                            skip_internal_scrolls=True,
                            reset_generation=self.reset_generation,
                        )
                    )
                return
            self.activate_container(cached, pre_reveal=True, keep_outgoing=True)
            self.refresh_match_scrollbar(chunks)
            self.show_progress_bar(total=1, progress=0, phase="rendering…")
            self._app.call_after_refresh(self.finalize_pre_reveal, cached, focus_chunk_seq)
            return

        # Either no container yet OR a partially-mounted one (resume).
        # Either way, kick off the mount task; show the bar immediately
        # so the user sees feedback before the task even starts.
        if cached is None:
            container = PreviewContainer(
                parent_doc_id=parent_id,
                query_signature=query_sig,
                total_chunks=len(chunks),
            )
        else:
            container = cached
        self.show_progress_bar(
            total=len(chunks),
            progress=len(container.mounted_indices),
            phase="mounting…",
        )
        self.mount_task = asyncio.create_task(
            self._mount_chunks_async(
                parent_id,
                focus_chunk_seq,
                chunks,
                container,
                reset_generation=self.reset_generation,
            )
        )

    def dispatch_flat_mount(
        self,
        parent_id: str,
        focus_chunk_seq: int,
        chunks: list[FileChunk],
        *,
        prebuilt: RenderedDocument | None = None,
    ) -> None:
        """Flat-buffer mount: resolve doc (cache > prebuilt > main-thread
        build), install into the shared widget, activate."""
        query_sig = self._app._search.query_signature()
        cache_key = (parent_id, query_sig)

        doc = self._app._flat.cache.get(cache_key)
        cache_hit = doc is not None
        if doc is None:
            doc = prebuilt
        if doc is None:
            try:
                pane_widget = self._app.query_one("#preview_pane", VerticalScroll)
                measured = pane_widget.content_size.width - 1
                wrap_width = max(20, measured) if measured > 0 else 0
            except Exception:
                wrap_width = 0
            fv = self._app._flat.build_file_view(chunks)
            doc = build_rendered_document(fv, wrap_width=wrap_width)

        self._app._flat.cache[cache_key] = doc
        self._app._flat.cache.move_to_end(cache_key)
        while len(self._app._flat.cache) > tuning.PREVIEW_CACHE_MAX_FILES:
            self._app._flat.cache.popitem(last=False)

        # A post-query auto-park can arrive pointing at a chunk that BM25
        # matched but carries no highlightable span (e.g. a tree-rebuild
        # cursor echo lands on chunk 0). Scrolling there parks the view at
        # the chunk's top with nothing highlighted, and a second racing
        # dispatch then clobbers the correct match scroll — last writer
        # wins, non-deterministically. Resolve to the file's first matching
        # chunk so every dispatch for this (file, query) lands on the same
        # match regardless of arrival order. Genuine match chunks (real
        # section navigation) and the no-match browse case are left as-is.
        if doc.fv.first_hit_line_in_chunk and focus_chunk_seq not in doc.fv.first_hit_line_in_chunk:
            focus_chunk_seq = min(doc.fv.first_hit_line_in_chunk)

        buf = self._app._flat.ensure_shared_buffer()
        if self._app._flat.installed_key != cache_key:
            # New doc: install + synchronous no-flash scroll to the match.
            self._app._flat.install_doc(
                buf,
                doc,
                focus_chunk_seq,
                parent_id=parent_id,
                context_fraction=tuning.MATCH_CONTEXT_FRACTION,
            )
            self._app._flat.installed_key = cache_key
        self._app._flat.activate(buf)
        # Route the flat match scroll through the controller: arm with the
        # resolved focus chunk and reconcile (idempotent — re-applies the
        # install's scroll; for intra-file nav it IS the scroll). The 25%
        # context margin matches the structural path.
        self._app._preview_scroll.arm(ScrollAnchor(parent_id, focus_chunk_seq))
        self._app._preview_scroll.reconcile()
        self._app._diag_log(
            f"dispatch_flat parent={parent_id[:8]} cache_hit={'yes' if cache_hit else 'no'} "
            f"prebuilt={'yes' if prebuilt is not None else 'no'} strips={len(doc.strips)} "
            f"wrap_width={doc.wrap_width} chunks={len(chunks)}"
        )
        self.hide_progress_bar()
        self.parent_id = parent_id
        self._app._refresh_status()

    def show_progress_bar(
        self,
        *,
        total: int | None,
        progress: int = 0,
        phase: str | None = None,
    ) -> None:
        """Open or update the progress session for a preview load. Determinate
        only — ``total=None`` is treated as ``total=1`` so the indeterminate
        red pulse never paints."""
        total_eff = total if (total is not None and total > 0) else 1
        s = self._app._progress.active
        if s is None or s.closed:
            s = self._app._progress.open(phase or "loading…", total=total_eff)
        else:
            if phase is not None:
                s.set_phase(phase)
            s.set_total(total_eff)
        s.set_progress(progress)
        import contextlib

        # Pane's own scrollbar would jitter as virtual_size grows; the strip
        # below the layout carries the loading signal instead.
        with contextlib.suppress(Exception):
            self._app.query_one("#preview_pane", VerticalScroll).add_class("is-loading")

    def hide_progress_bar(self) -> None:
        """Close the active session + re-enable pane scrolling. Idempotent."""
        s = self._app._progress.active
        if s is not None and not s.closed:
            s.close()
        import contextlib

        with contextlib.suppress(Exception):
            self._app.query_one("#preview_pane", VerticalScroll).remove_class("is-loading")

    def update_progress_bar(self, progress: int) -> None:
        s = self._app._progress.active
        if s is not None and not s.closed:
            s.set_progress(progress)

    def clear_pane_placeholder(self) -> None:
        """Drop the empty-state Static. Called by every activate path so the
        placeholder never paints above a real preview."""
        import contextlib

        with contextlib.suppress(Exception):
            pane = self._app.query_one("#preview_pane", VerticalScroll)
            for w in list(pane.children):
                if isinstance(w, Static) and w.id == "placeholder":
                    with contextlib.suppress(Exception):
                        w.remove()

    def activate_container(
        self,
        container: PreviewContainer,
        *,
        pre_reveal: bool = False,
        keep_outgoing: bool = False,
    ) -> None:
        """Make ``container`` the only visible preview. With
        ``pre_reveal=True`` the container is laid out but invisible
        (opacity:0) until the scroll lands — no flash to file-top before
        the jump-to-match. With ``keep_outgoing=True`` the previously-active
        container stays visible (so the pane never blanks) until the atomic
        reveal swaps to ``container`` (see :meth:`_swap_reveal_target`)."""
        from fnd.tui import _perf

        self.clear_pane_placeholder()
        # Hold the outgoing preview on screen while the incoming one builds
        # invisibly; the reveal swap hides it and shows the new one in one tick.
        # Only keep a genuinely-visible prior container (not one left invisible
        # by a superseded mount) — otherwise the pane would blank anyway.
        prior = self.active
        outgoing = (
            prior
            if keep_outgoing
            and pre_reveal
            and prior is not None
            and prior is not container
            and not prior.has_class("-pre-reveal")
            and not prior.has_class("-hidden")
            else None
        )
        self.outgoing = outgoing
        for child in self._app.query(PreviewContainer):
            if child is container:
                child.remove_class("-hidden")
                if pre_reveal:
                    child.add_class("-pre-reveal")
                else:
                    child.remove_class("-pre-reveal")
            elif child is outgoing:
                # Keep visible until the swap; don't disturb its scroll.
                child.remove_class("-hidden")
                child.remove_class("-pre-reveal")
            else:
                child.add_class("-hidden")
                child.remove_class("-pre-reveal")
        for child in self._app.query(LineBufferPreview):
            child.add_class("-hidden")
        self.active = container
        self._app._flat.active_buffer = None
        if not pre_reveal:
            _perf.mark(
                "click_to_display_end",
                parent_id=container.parent_doc_id,
                path="structural_immediate",
            )
        self.parent_id = container.parent_doc_id
        self.chunk_widgets = container.chunk_widgets
        self.match_targets = container.match_targets
        # Cache-hit paths return without _mount_chunks_async (which is
        # where _refresh_status normally fires at the end); refresh here
        # so the pane title swaps to the activated file immediately.
        self._app._refresh_status()

    # ── StructuralHost / FlatHost implementation ───────────────────
    # The scroll strategies read the pane, chunk/match maps, match spec
    # and reveal/reconcile gates through these.

    def preview_pane(self) -> VerticalScroll:
        return self._app.query_one("#preview_pane", VerticalScroll)

    def effective_match_spec(self) -> MatchSpec:
        return self._app._effective_match_spec

    def diag_log(self, msg: str) -> None:
        self._app._diag_log(msg)

    def call_after_refresh(self, callback: Callable[..., Any], *args: Any, **kwargs: Any) -> object:
        return self._app.call_after_refresh(callback, *args, **kwargs)

    def active_flat_buffer(self) -> LineBufferPreview | None:
        return self._app._flat.active_buffer

    def begin_reconcile_scroll(self) -> None:
        self.reconciling = True

    def end_reconcile_scroll(self) -> None:
        self.reconciling = False

    def swap_reveal_target(self, target: Widget, margin: int) -> bool:
        """Atomic preview swap: hide the outgoing container, position the
        incoming one so ``target`` sits ``margin`` rows down, and reveal it —
        all in one tick. Returns True when a swap happened, False when there is
        no outgoing container (the caller then scrolls + reveals normally).

        The outgoing container stayed on screen through the whole build, so the
        first frame the user sees after this is the new preview already at its
        match — no blank, no scroll-into-place. ``target``'s offset is taken
        relative to the incoming container's top, which is scroll-independent
        and so survives the outgoing container leaving the layout."""
        outgoing = self.outgoing
        new = self.active
        if outgoing is None or new is None or outgoing is new:
            return False
        offset = target.region.y - new.region.y
        target_y = max(0, offset - margin)
        pane = self._app.query_one("#preview_pane", VerticalScroll)
        outgoing.add_class("-hidden")
        pane.scroll_to(y=target_y, animate=False, immediate=True)
        new.remove_class("-pre-reveal")
        self.outgoing = None
        return True

    def reveal(self, container: PreviewContainer) -> None:
        """Reveal ``container`` and drop any still-held outgoing preview.
        Fallback for paths where :meth:`swap_reveal_target` did not run (no
        match resolved, or no outgoing) — a no-op for the class already lifted
        by the swap.

        Guard: a finalize/reveal callback is queued via ``call_after_refresh``
        and runs a tick later. If a newer navigation superseded this mount in
        the meantime, ``container`` is no longer ``_active_preview`` — revealing
        it would surface the wrong file and clobber the new nav's outgoing
        reference. Detached finalize tasks aren't cancelled, so this staleness
        check (not task cancellation) is the single point that makes a
        superseded reveal a no-op."""
        if container is not self.active:
            return
        outgoing = self.outgoing
        if outgoing is not None and outgoing is not container:
            outgoing.add_class("-hidden")
        self.outgoing = None
        container.remove_class("-pre-reveal")

    def finalize_pre_reveal(self, container: PreviewContainer, focus_chunk_seq: int) -> None:
        """Lift ``-pre-reveal`` once focused chunk's compose is ready, then scroll."""
        import time

        t0 = time.perf_counter()
        self._app._diag_log(
            f"finalize_pre_reveal start seq={focus_chunk_seq} parent_id={container.parent_doc_id}"
        )

        self._do_finalize_pre_reveal(container, focus_chunk_seq, retries=10, t0=t0)

    async def _finalize_via_lock(
        self,
        container: PreviewContainer,
        focus_chunk_seq: int,
        t0: float,
        *,
        expected_above_seqs: list[int] | None = None,
        path: str = "cold_via_lock",
    ) -> None:
        """Wait for *every* chunk above the focus in the mounted window
        to finish building before revealing + scrolling. Awaiting only
        the focus chunk's ``build_done`` (the previous behaviour) let
        the scroll land while siblings above were still height=0; once
        those grew, the focus chunk's virtual_y shifted and the user
        saw the correct match flash, then jump to an unrelated area.
        Waiting for the above-siblings means the focus chunk's
        virtual_y is final at scroll time."""
        import asyncio
        import time

        from fnd.tui import _perf

        _fin_t0 = time.perf_counter()
        header = container.chunk_widgets.get(focus_chunk_seq)
        # Step 1: wait for the focus chunk's build.
        try:
            async with asyncio.timeout(8.0):
                if isinstance(header, FNDMarkdown):
                    await header.build_done.wait()
        except TimeoutError:
            self._app._diag_log(
                f"finalize_via_lock focus build_done timeout seq={focus_chunk_seq} path={path}"
            )
        # Step 2: wait for the above-window chunks to be MOUNTED, then built.
        # We cannot just read chunk_widgets now: when the focus chunk was
        # prefetched its build_done is already set, so Step 1 returns before
        # Phase 1b has mounted the window — chunk_widgets would hold only the
        # focus chunk (above_waited=0), the scroll would land against a
        # focus-at-top layout, and the view would settle-scroll once the real
        # above content mounts. Yield until every expected above seq exists.
        expected = [s for s in (expected_above_seqs or []) if s < focus_chunk_seq]
        try:
            async with asyncio.timeout(8.0):
                while not all(s in container.chunk_widgets for s in expected):
                    await asyncio.sleep(0)
        except TimeoutError:
            self._app._diag_log(
                f"finalize_via_lock above mount timeout seq={focus_chunk_seq} "
                f"expected={len(expected)} path={path}"
            )
        above_widgets: list[FNDMarkdown] = [
            w
            for seq, w in container.chunk_widgets.items()
            if seq < focus_chunk_seq and isinstance(w, FNDMarkdown)
        ]
        if above_widgets:
            try:
                async with asyncio.timeout(8.0):
                    await asyncio.gather(*(w.build_done.wait() for w in above_widgets))
            except TimeoutError:
                self._app._diag_log(
                    f"finalize_via_lock above build_done timeout "
                    f"seq={focus_chunk_seq} above_count={len(above_widgets)} path={path}"
                )
        # Wait for the screen to FULLY settle before scrolling. build_done only
        # says the markdown rendered; the compositor's arrange (which fixes every
        # chunk's region AND the pane's scroll extent) runs over several more
        # refreshes. The old region.height>0 poll only checked the focus chunk
        # and raced the chunks above it still flowing — so a deep match scrolled
        # against a half-settled layout and clamped off-screen. _await_preview_settled
        # is Textual's own message-drain signal (what Pilot waits on): it returns
        # only once every widget has processed its pending layout, so the geometry
        # the scroll reads is final.
        _perf.mark(
            "finalize_buildwait",
            ms=(time.perf_counter() - _fin_t0) * 1000.0,
            above=len(above_widgets),
            path=path,
        )
        import os as _os

        # Option B: scoped settle is the default — wait only on the geometry the
        # scroll reads (focus + above-window heights), not a full-pane drain.
        # _FND_FULL_SETTLE=1 restores the old behaviour as an escape hatch.
        if _os.environ.get("_FND_FULL_SETTLE") == "1":
            await self.await_settled()
        else:
            await self.await_match_settled(header, above_widgets)
        wait_ms = (time.perf_counter() - t0) * 1000
        self.hide_progress_bar()
        _perf.mark(
            "click_to_display_end",
            parent_id=container.parent_doc_id,
            focus_seq=focus_chunk_seq,
            path=path,
        )

        def _reveal_when_landed() -> None:
            self.reveal(container)

        # Scroll while the container is still invisible (opacity:0), then reveal
        # once it lands — so the match never flashes at the file top first. The
        # layout is settled, so this is a single deterministic scroll.
        self._app._preview_scroll.reconcile(_reveal_when_landed)
        # This render has settled — release the in-flight coalescing
        # latch so a later genuine re-render of the same target can run.
        self.inflight_target = None
        self._app._diag_log(
            f"finalize_via_lock done seq={focus_chunk_seq} path={path} "
            f"wait_ms={wait_ms:.1f} above_waited={len(above_widgets)}"
        )

    def _do_finalize_pre_reveal(
        self,
        container: PreviewContainer,
        focus_chunk_seq: int,
        retries: int,
        t0: float,
    ) -> None:
        import time

        from fnd.tui import _perf

        header = container.chunk_widgets.get(focus_chunk_seq)
        compose_done = True
        if header is not None and hasattr(header, "first_match_block"):
            compose_done = header.first_match_block is not None  # pyright: ignore[reportAttributeAccessIssue]
        if not compose_done and retries > 0:
            self._app.call_after_refresh(
                self._do_finalize_pre_reveal,
                container,
                focus_chunk_seq,
                retries - 1,
                t0,
            )
            return

        wait_ms = (time.perf_counter() - t0) * 1000
        self.hide_progress_bar()
        _perf.mark(
            "click_to_display_end",
            parent_id=container.parent_doc_id,
            focus_seq=focus_chunk_seq,
            path="warm_pre_reveal",
        )

        def _reveal_when_landed() -> None:
            self.reveal(container)
            self._app._diag_log(
                f"finalize_pre_reveal done seq={focus_chunk_seq} "
                f"wait_ms={wait_ms:.1f} elapsed_ms={(time.perf_counter() - t0) * 1000:.1f} "
                f"compose_done={compose_done}"
            )

        # Wait for the screen to fully settle, THEN scroll once + reveal — same
        # deterministic settle the cold path uses (see _finalize_via_lock). The
        # warm reveal is sync, so run the await in a task.
        import asyncio as _asyncio

        async def _settled_reconcile() -> None:
            await self.await_settled()
            self._app._preview_scroll.reconcile(_reveal_when_landed)

        # Cancel a prior settle-await on this container before replacing it — a
        # rapid re-nav would otherwise leave it running, burning a full DOM-drain
        # and a redundant (generation-guarded) reconcile. Safe to cancel: this
        # task does no cleanup, so CancelledError just unwinds the await. Held on
        # the container so GC can't collect the new one mid-await (RUF006).
        _prior = getattr(container, "_finalize_task", None)
        if _prior is not None and not _prior.done():
            _prior.cancel()
        container._finalize_task = _asyncio.create_task(_settled_reconcile())  # type: ignore[attr-defined]

    async def await_settled(self, max_rounds: int = 10) -> None:
        """Deterministically wait until the screen has processed all pending
        layout messages, so the preview geometry is final before we scroll.

        Drain = Textual's own settle mechanism (what ``Pilot.pause`` /
        ``_wait_for_screen`` use): schedule a callback on every widget via
        ``call_later`` and wait for them all to fire — i.e. every widget has
        processed the messages queued now. One drain settles the current wave;
        the reflow it triggers posts a follow-up wave, so loop until the screen
        reports no pending layout/repaint/recompose (its ``_on_idle`` condition),
        bounded by ``max_rounds``. Replaces stability-polling heuristics, which
        can't tell a settled layout from a mid-reflow plateau."""
        import asyncio
        import time as _time

        from fnd.tui import _perf

        _t0 = _time.perf_counter()
        rounds = 0
        walked = 0
        reason = "max_rounds"
        try:
            for _ in range(max_rounds):
                try:
                    screen = self._app.screen
                except Exception:
                    reason = "no_screen"
                    return
                # Drain only what bears on the preview's geometry: the app + screen
                # (which run the arrange) and the preview pane's own subtree — NOT the
                # whole screen (results tree, sidebars), which is irrelevant here and
                # makes the per-round callback count scale with the unrelated DOM.
                try:
                    pane = self._app.query_one("#preview_pane", VerticalScroll)
                    children = [self._app, screen, *pane.walk_children(with_self=True)]
                except Exception:
                    # No pane yet — fall back to the screen-wide drain.
                    children = [self._app, *screen.walk_children(with_self=True)]
                count = 0
                done = asyncio.Event()

                def _dec(_done: asyncio.Event = done) -> None:
                    nonlocal count
                    count -= 1
                    if count == 0:
                        _done.set()

                for child in children:
                    if child.call_later(_dec):
                        count += 1
                rounds += 1
                walked = len(children)
                if count:
                    try:
                        async with asyncio.timeout(5.0):
                            await done.wait()
                    except TimeoutError:
                        reason = "timeout"
                        return
                # Stop once the screen has no pending layout work — the geometry is
                # now final. (These are the flags Screen._on_idle itself checks.)
                if not (
                    getattr(screen, "_layout_required", False)
                    or getattr(screen, "_repaint_required", False)
                    or getattr(screen, "_recompose_required", False)
                    or getattr(screen, "_dirty_widgets", None)
                ):
                    reason = "settled"
                    return
        finally:
            _perf.mark(
                "settle",
                rounds=rounds,
                walked=walked,
                ms=(_time.perf_counter() - _t0) * 1000.0,
                reason=reason,
            )

    async def await_match_settled(
        self,
        header: FNDMarkdown | Widget | None,
        above_widgets: list[FNDMarkdown],
        max_rounds: int = 12,
    ) -> None:
        """Option B — targeted settle. The full-pane drain waits for the WHOLE
        screen to stop reflowing; but the only geometry the scroll reads is the
        focus chunk's virtual_y, which is fixed once the above-window chunk
        heights stop changing. So drain only [app, screen, focus, above] and
        exit when those heights are stable for two consecutive rounds — far
        fewer callbacks/round than walking every block in the pane, and an
        earlier exit than the screen-global flags allow. Stability is judged on
        the SPECIFIC heights that move the match, not a generic region poll, so
        a mid-reflow plateau can't masquerade as settled (the heights are still
        changing during reflow)."""
        import asyncio
        import time as _time

        from fnd.tui import _perf

        _t0 = _time.perf_counter()
        watch: list[Widget] = [w for w in [header, *above_widgets] if w is not None]
        # Nothing to track (no focus/above widgets, or no resolvable match) —
        # fall back to the full-pane drain rather than scroll against unknown
        # geometry. The scoped path only buys us anything when there ARE heights
        # to watch settle.
        if not watch:
            await self.await_settled()
            return
        try:
            screen = self._app.screen
        except Exception:
            return  # no screen (teardown / transition) — nothing to settle
        targets = [self._app, screen, *watch]  # App + Screen + widgets all have call_later

        def _sig() -> tuple[int, ...]:
            out: list[int] = []
            for w in watch:
                try:
                    out.append(w.size.height)
                except Exception:
                    out.append(-1)
            return tuple(out)

        prev: tuple[int, ...] | None = None
        stable = 0
        rounds = 0
        reason = "max_rounds"
        try:
            for _ in range(max_rounds):
                count = 0
                done = asyncio.Event()

                def _dec(_done: asyncio.Event = done) -> None:
                    nonlocal count
                    count -= 1
                    if count == 0:
                        _done.set()

                for w in targets:
                    if w.call_later(_dec):
                        count += 1
                rounds += 1
                if count:
                    try:
                        async with asyncio.timeout(5.0):
                            await done.wait()
                    except TimeoutError:
                        reason = "timeout"
                        return
                cur = _sig()
                # All watched heights must be real (>0) AND unchanged twice.
                if cur == prev and all(h > 0 for h in cur):
                    stable += 1
                    if stable >= 2:
                        reason = "stable"
                        return
                else:
                    stable = 0
                prev = cur
        finally:
            _perf.mark(
                "settle",
                rounds=rounds,
                walked=len(targets),
                ms=(_time.perf_counter() - _t0) * 1000.0,
                reason=reason,
                scoped=True,
            )

    async def _settled_instant_scroll(
        self, container: PreviewContainer, parent_id: str, focus_chunk_seq: int
    ) -> None:
        """Option C: the target chunk is already mounted, so scroll straight to
        it — but settle the focus + nearest-above heights first (cheap, ~2 rounds
        when the file is idle) so heavy table/fence geometry is final and the
        match lands on-screen instead of clamping off."""
        from fnd.tui import _perf

        header = container.chunk_widgets.get(focus_chunk_seq)
        above_seqs = sorted(s for s in container.chunk_widgets if s < focus_chunk_seq)[-7:]
        above = [
            w for s in above_seqs if isinstance((w := container.chunk_widgets.get(s)), FNDMarkdown)
        ]
        await self.await_match_settled(header, above)
        _perf.mark("click_to_display_end", parent_id=parent_id, path="already_active_scroll_only")
        self._app._preview_scroll.reconcile()

    def bump_reset_generation(self) -> None:
        """Invalidate any in-flight mount. Call from every path that clears the
        preview caches + DOM (new query, scope clear, highlight rerender): the
        cancelled mount's deferred `finally` sees the changed generation and
        drops its now-stale container instead of re-caching it. Navigation /
        resume paths that cancel a mount but keep the cache must NOT call this —
        their partial container is still wanted for a later resume."""
        self.reset_generation += 1

    def cancel_mount_task(self) -> None:
        """Cancel any in-flight mount task. The cancelled task's
        partial-mount state lives on its :class:`PreviewContainer`,
        so a later visit can resume it."""
        import contextlib

        task = self.mount_task
        if task is None:
            return
        try:
            done = task.done()  # type: ignore[attr-defined]
        except Exception:
            done = True
        if not done:
            with contextlib.suppress(Exception):
                task.cancel()  # type: ignore[attr-defined]
        self.mount_task = None

    def on_load_failed(self, exc: BaseException) -> None:
        """Worker error callback. Hide the bar, surface a notify."""
        self.hide_progress_bar()
        self._app.notify(f"Preview load failed: {exc}", severity="error")

    def on_chunks_loaded(
        self,
        parent_id: str,
        focus_chunk_seq: int,
        chunks: list[FileChunk],
        prebuilt: RenderedDocument | None = None,
    ) -> None:
        """Worker callback. Caches chunks + (optional) flat-path bundle;
        re-enters the mount path."""
        self.chunk_cache[parent_id] = chunks
        if prebuilt is not None:
            # Cache the bundle so a later visit to the same file in the
            # same query can install it without re-decoding or re-
            # rendering. Same key as ``_flat_buffer_cache``.
            self.prebuilt_cache[(parent_id, self._app._search.query_signature())] = prebuilt
        if not chunks:
            # Empty file — hide bar, leave pane blank.
            self.hide_progress_bar()
            self.parent_id = parent_id
            self._app._refresh_status()
            return
        self.dispatch_mount(parent_id, focus_chunk_seq, chunks, prebuilt=prebuilt)

    def user_mount_in_flight(self) -> bool:
        task = self.mount_task
        if task is None:
            return False
        try:
            return not task.done()  # type: ignore[attr-defined]
        except Exception:
            return False

    async def _mount_chunks_async(
        self,
        parent_id: str,
        focus_chunk_seq: int,
        chunks: list[FileChunk],
        container: PreviewContainer,
        *,
        skip_internal_scrolls: bool = False,
        reset_generation: int,
    ) -> None:
        """Visible-first mount + hidden-prepend background fill.

        Phase 1 (sync, fast): mount the focused chunk plus
        :data:`tuning.VISIBLE_FIRST_ABOVE` chunks above and
        :data:`tuning.VISIBLE_FIRST_BELOW` below — a window roughly matching
        the typical viewport. The user sees the relevant content
        instantly.

        Phase 2a (async, batched): append the remaining chunks BELOW
        the visible window in document order. These add to virtual
        size but don't shift the visible viewport.

        Phase 2b (async, batched): mount the chunks ABOVE the visible
        window, but set ``display = False`` on each newly-mounted
        widget the moment it lands. Hidden widgets contribute zero
        layout, so the focused chunk's screen position stays put while
        the background fill runs (no jumping). After the last above-
        window chunk is mounted, we reveal the entire batch at once
        and re-anchor scroll to the focused chunk's top edge.

        Cancellation is non-destructive: partial state lives on
        ``container.mounted_indices``. The ``finally`` block always
        reveals any still-hidden widgets so a cancelled task doesn't
        leave the container in a half-hidden state.
        """
        import asyncio
        import contextlib

        pane = self._app.query_one("#preview_pane", VerticalScroll)

        # ``reset_generation`` was snapshotted by the caller AT create_task time
        # (coroutine args evaluate eagerly), so it predates any reset that lands
        # before this body's first slice runs. If a new query / scope clear /
        # rerender bumps it while we're in flight, our finally drops this
        # container instead of re-caching it back into the just-cleared state.
        my_generation = reset_generation

        needs_pre_reveal = container.parent is None or container.has_class("-hidden")
        # Newly-mounted "above-window" widgets get hidden until phase 2b
        # finishes; the finally block makes sure every entry in this
        # list ends up displayed even on cancellation.
        hidden_widgets: list[Widget] = []

        # The try MUST cover the early awaits below (container mount,
        # cancel_task_on): they run BEFORE the detached finalize task is
        # spawned, and a cancellation here would otherwise skip the finally
        # entirely — stranding the progress bar with no task left to hide it.
        try:
            if container.parent is None:
                await pane.remove_children("#placeholder")
                await pane.mount(container)
            else:
                await pane.remove_children("#placeholder")
            await self._app._prefetch.cancel_task_on(container)
            self.activate_container(
                container, pre_reveal=needs_pre_reveal, keep_outgoing=needs_pre_reveal
            )
            cold_mount = needs_pre_reveal
            self.refresh_match_scrollbar(chunks)

            # Establish the focused window indices (clamped to chunks).
            focus_idx = next(
                (i for i, c in enumerate(chunks) if c.chunk_seq == focus_chunk_seq),
                0,
            )
            win_start = max(0, focus_idx - tuning.VISIBLE_FIRST_ABOVE)
            win_end = min(len(chunks), focus_idx + tuning.VISIBLE_FIRST_BELOW + 1)

            # Phase 1a: mount the focused chunk first and yield so it
            # paints before the surrounding context mounts. On large
            # files the rest of the visible window can take several
            # hundred ms to mount; the user clicked a specific match
            # and should see THAT chunk's content first, not stare at a
            # progress bar while neighbouring chunks slowly fill in.
            if focus_idx not in container.mounted_indices:
                self.mount_chunk_into(container, chunks[focus_idx], focus_idx, chunks)
            # Event-based finalize: parallel task awaits the focused
            # chunk widget's lock (Markdown.update build-done signal)
            # before scrolling. Replaces the polling retry chain which
            # raced layout on heavy md (cold) AND lost the scroll on
            # out-of-window same-file navigation (warm-resume) because
            # the freshly-mounted chunk's region was still 0 when the
            # 30-retry budget expired.
            if cold_mount or not skip_internal_scrolls:
                import time as _time

                # Reference held on the container so GC doesn't collect
                # the task mid-await (RUF006). Cleared once it completes.
                # The above-window chunks Phase 1b will mount. finalize must
                # wait for THESE to exist + build, not just whatever is in
                # chunk_widgets when it first looks — a prefetched focus chunk
                # has build_done already set, so finalize would otherwise run
                # before Phase 1b mounts the window and scroll to a stale
                # (focus-at-top) position, then settle-scroll once they land.
                expected_above_seqs = [chunks[i].chunk_seq for i in range(win_start, focus_idx)]
                _finalize_task = asyncio.create_task(
                    self._finalize_via_lock(
                        container,
                        focus_chunk_seq,
                        _time.perf_counter(),
                        expected_above_seqs=expected_above_seqs,
                        path="cold_via_lock" if cold_mount else "warm_via_lock",
                    )
                )
                container._finalize_task = _finalize_task  # type: ignore[attr-defined]
            self.update_progress_bar(progress=len(container.mounted_indices))
            await asyncio.sleep(0)

            # Phase 1b: mount the visible window. Closest-to-focus first.
            max_offset = max(focus_idx - win_start, win_end - 1 - focus_idx)
            for offset in range(1, max_offset + 1):
                below = focus_idx + offset
                if below < win_end and below not in container.mounted_indices:
                    self.mount_chunk_into(container, chunks[below], below, chunks)
                above = focus_idx - offset
                if win_start <= above and above not in container.mounted_indices:
                    self.mount_chunk_into(container, chunks[above], above, chunks)
            self.update_progress_bar(progress=len(container.mounted_indices))
            await asyncio.sleep(0)

            # Phase 2a: background fill BELOW the window, capped at the
            # lazy-mount radius. Kept SMALL so first paint only needs the
            # window — Option C's full-mount is deferred to Phase 3, strictly
            # after the reveal, so it never delays first paint.
            below_end = min(len(chunks), focus_idx + 1 + tuning.BACKGROUND_FILL_RADIUS)
            for i in range(win_end, below_end):
                if i in container.mounted_indices:
                    continue
                self.mount_chunk_into(container, chunks[i], i, chunks)
                self.update_progress_bar(progress=len(container.mounted_indices))
                await asyncio.sleep(0.002)
            await asyncio.sleep(0)

            # Phase 2b: hidden-prepend ABOVE the window, capped at the
            # same radius. display=False keeps the focused chunk
            # anchored while earlier sections mount.
            above_start = max(0, focus_idx - tuning.BACKGROUND_FILL_RADIUS)
            for i in range(win_start - 1, above_start - 1, -1):
                if i in container.mounted_indices:
                    continue
                before = set(container.children)
                self.mount_chunk_into(container, chunks[i], i, chunks)
                for w in container.children:
                    if w not in before:
                        w.display = False
                        hidden_widgets.append(w)
                self.update_progress_bar(progress=len(container.mounted_indices))
                # Wall-clock yield — see prefetch loop.
                await asyncio.sleep(0.002)

            # Reveal + anchor in one synchronous block so Textual
            # folds both layout changes into a single paint — no
            # visible "shift down then scroll back up" sequence.
            if hidden_widgets:
                for w in hidden_widgets:
                    w.display = True
                hidden_widgets.clear()
                if not skip_internal_scrolls and focus_chunk_seq in container.chunk_widgets:
                    # Revealing the above-window chunks shifted the layout, so
                    # the focus chunk must be re-anchored. Scroll to the MATCH
                    # (first_match_block), not the chunk's top edge — anchoring
                    # to the top pushes a match deep inside the chunk off-screen
                    # the moment the background fill completes (the cold-load
                    # "wrong position until expanded" symptom).
                    with contextlib.suppress(Exception):
                        self._app._preview_scroll.reconcile()

            # Phase 3 (Option C): the first view has now painted (finalize
            # revealed during Phase 1/2). Fill the REST of the file in the
            # background so internal match-jumps land on an already-mounted
            # chunk. Strictly AFTER the reveal so it never delays first paint;
            # outward in small batches via _lazy_mount_batch (which keeps the
            # view anchored when prepending above); generous yields so it never
            # starves interaction; budget-capped so monster files stay windowed;
            # bails the instant the user navigates away.
            if len(chunks) <= tuning.FULLMOUNT_CHUNK_BUDGET:
                # Wait for finalize to actually reveal (first paint) before adding
                # any DOM — otherwise this fill runs on the same coroutine and
                # starves the finalize task, delaying first paint several-fold.
                _ft = getattr(container, "_finalize_task", None)
                if _ft is not None:
                    with contextlib.suppress(Exception):
                        await _ft
                await asyncio.sleep(0.05)
                batch_size = 6
                # Fill BELOW only: appending in document order grows content
                # DOWNWARD, so the match the user is reading never moves — no
                # flicker. Downward match-jumps land on a mounted chunk (instant).
                # We deliberately DON'T pre-fill ABOVE: inserting content above the
                # viewport shoves it down, and the scroll can only re-pin a frame
                # later (layout is async), so a passive above-fill always jitters
                # the viewport. Upward jumps instead rebuild on demand (~140ms,
                # correct, flicker-free) — movement during a deliberate jump is
                # expected; movement while the user sits still is not.
                # Empty-guard (degenerate mount); and bail the moment the user
                # takes scroll control (a user scroll clears is_armed) so upward
                # lazy-mount isn't walled behind this background below-fill —
                # once we stop, _preview_mount_task completes and lazy-mount
                # handles both directions on demand.
                i = (
                    (max(container.mounted_indices) + 1)
                    if container.mounted_indices
                    else len(chunks)
                )
                while (
                    i < len(chunks)
                    and self.active is container
                    and self._app._preview_scroll.is_armed
                ):
                    if i not in container.mounted_indices:
                        with contextlib.suppress(Exception):
                            self.mount_chunk_into(container, chunks[i], i, chunks)
                    i += 1
                    if i % batch_size == 0:
                        await asyncio.sleep(0.006)
        finally:
            # Always reveal any widgets we hid; a cancelled task that
            # left them hidden would leak a half-displayed container
            # into the cache.
            for w in hidden_widgets:
                with contextlib.suppress(Exception):
                    w.display = True
            superseded = self.reset_generation != my_generation
            if superseded:
                # A new query / scope clear / rerender cleared the caches AND the
                # DOM while this mount was in flight, then cancelled us. Re-
                # caching or leaving this container mounted would re-pollute the
                # just-cleared pane with the previous query's half-built widget
                # tree — the "stuck mid-mount after a new query" bug. Drop it.
                self.diag_log(
                    f"mount superseded gen={my_generation}->{self.reset_generation} "
                    f"parent={container.parent_doc_id[:8]} — dropping stale container"
                )
                # The cold path spawns a DETACHED _finalize_via_lock task that, on
                # completion, unconditionally hides the progress bar and clears
                # inflight_target. Cancelling the mount task does NOT cancel it, so
                # a superseded mount's finaliser would later clobber the SUCCESSOR
                # query's bar + latch. Cancel it here before dropping the widget.
                _ft = getattr(container, "_finalize_task", None)
                if _ft is not None and not _ft.done():
                    _ft.cancel()
                with contextlib.suppress(Exception):
                    container.remove()
                if self.active is container:
                    self.active = None
                # Don't leave a removed widget dangling as the outgoing
                # (held-visible-during-swap) reference for the next reveal.
                if self.outgoing is container:
                    self.outgoing = None
            else:
                # Cache the container even when the mount didn't run to
                # completion. For monster files (1000+ page PDFs with
                # thousands of chunks) the user reliably navigates away
                # before is_complete becomes True; without caching the
                # partial container, every revisit re-mounts from scratch
                # and the file looks like it has no cache. The resume path
                # in ``_dispatch_preview_mount`` skips already-mounted
                # indices so partial-cache hits paint the previously-
                # mounted region instantly and continue the fill in the
                # background. The container we just mounted IS the active one,
                # so it's protected from its own eviction by definition (it just
                # got moved to the MRU slot).
                evicted = self.preview_cache.put(container, protect=container)
                for old in evicted:
                    with contextlib.suppress(Exception):
                        old.remove()
            if container.is_complete and not superseded:
                self.hide_progress_bar()
            elif (
                getattr(container, "_finalize_task", None) is None
                and (self.mount_task is None or self.mount_task is asyncio.current_task())
                and self.inflight_target in (None, (parent_id, focus_chunk_seq))
            ):
                # Ended in the early-await phase — before the detached finalize
                # task (the ONLY thing that hides the bar + releases the in-flight
                # latch on success) was spawned — and no successor took over the
                # loading state. Three ownership checks, all required:
                #   * ``_finalize_task is None``: no detached finalize will clear it.
                #   * ``mount_task is None`` (cancel_mount_task nulled it) OR
                #     ``is current_task()`` (exception / other-path end, where it
                #     still points at this now-dead task) — a successor MOUNT would
                #     have overwritten it.
                #   * ``inflight_target`` is still THIS target (or already clear):
                #     the uncached decode path cancels us and opens a NEW
                #     "decoding…" session WITHOUT assigning mount_task, so the
                #     latch already points at the successor — don't hide its bar.
                # Otherwise the bar stays "loading" until an unrelated navigation
                # dispatches a fresh load. Hide + release so a cancelled (or
                # failed) cold mount can't strand the preview.
                self.hide_progress_bar()
                self.inflight_target = None
            # Re-anchor only needed for cancellation case: a successful
            # Phase 2b reveal+anchor inline already scrolled to the
            # focused chunk. The inline anchor sees the post-reveal
            # widget y and lands accurately; an additional chained
            # anchor here would compete with the inline one and can
            # land at a slightly different y if Textual processes more
            # mounts in between, producing the "jump after settle" the
            # user reports.
            self._app._refresh_status()

    def mount_chunk_into(
        self,
        container: PreviewContainer,
        chunk: FileChunk,
        index: int,
        all_chunks: list[FileChunk],
    ) -> None:
        """Mount one chunk widget into ``container`` at the position
        implied by its index. Updates the container's
        ``mounted_indices`` / ``chunk_widgets`` / ``match_targets``
        bookkeeping."""
        # Find the smallest already-mounted index greater than this one
        # — we mount BEFORE that widget so chunks stay in document
        # order regardless of which phase mounts them.
        before_widget: Widget | None = None
        next_mounted = min(
            (j for j in container.mounted_indices if j > index),
            default=-1,
        )
        if next_mounted >= 0:
            before_seq = all_chunks[next_mounted].chunk_seq
            before_widget = container.chunk_widgets.get(before_seq)

        # Structural renderer (markdown widget) for formats whose
        # extractor populated body_md; per-line plain layout for
        # everything else (PDF, TXT). Save current widgets-by-chunk_seq
        # so the mount helpers fill the per-container dicts.
        if uses_markdown_renderer(chunk):
            self._mount_structured_chunk(container, chunk, before=before_widget)
        else:
            self._mount_plain_chunk(container, chunk, before=before_widget)
        container.mounted_indices.add(index)

    @property
    def scrollbar_markers_enabled(self) -> bool:
        """In-development scrollbar match highlighting — off unless the
        user opts in via ``[defaults] scrollbar_match_highlight``."""
        return bool(self._app._config and self._app._config.defaults.scrollbar_match_highlight)

    def refresh_match_scrollbar(self, chunks: list[FileChunk]) -> None:
        """Forward line-weighted match positions to the preview's custom
        scrollbar so markers sit where the matches actually render.

        Earlier this fed a bool-per-chunk map placed by chunk ordinal,
        which ignored chunk size — a match in a short chunk after a long
        one landed near the top instead of far down. ``structural_match_
        lines`` weights by each chunk's line count instead. On large
        markdown the lazy-mounted track spans only part of the file, so
        this stays behind the in-development toggle."""
        try:
            pane = self._app.query_one("#preview_pane", MatchAwareScroll)
        except Exception:
            return
        # Bump the generation up front: a clear or a new scan both supersede
        # any worker still running for the previous preview.
        self._markers_seq += 1
        if not self.scrollbar_markers_enabled:
            # Clear any markers a prior (enabled) load left, so toggling
            # the feature off takes effect on the next preview load.
            pane.set_match_lines([], 0)
            return
        # structural_match_lines scans every source line; a no/sparse-match
        # query on a large doc was measured in seconds (multi-minute worst
        # case). Run it off the event loop and apply when ready — stale
        # results (a newer nav bumped the generation) are dropped.
        token = self._markers_seq
        spec = self._app._effective_match_spec
        snapshot = list(chunks)

        def _scan() -> None:
            from fnd.tui.preview_markers import structural_match_lines

            match_lines, total_lines = structural_match_lines(snapshot, spec)

            def _apply() -> None:
                if token != self._markers_seq:
                    return
                # The pane can be mid-teardown by the time this lands (app quit
                # during a long scan); a failed marker update is never fatal.
                with contextlib.suppress(Exception):
                    pane.set_match_lines(match_lines, total_lines)

            self._app.call_from_thread(_apply)

        self._app.run_worker(_scan, thread=True, exclusive=True, group="preview-markers")

    def _mount_chunks_for_file(self, parent_id: str, chunks: list[FileChunk]) -> None:
        """Legacy synchronous mount path retained for tests that exercise
        the rendering surface directly. The interactive flow now uses
        :meth:`_mount_chunks_async` (visible-first + background fill);
        this entry point clears the pane and mounts everything at once
        into a fresh :class:`PreviewContainer`.
        """
        pane = self._app.query_one("#preview_pane", VerticalScroll)
        for w in list(pane.children):
            w.remove()
        container = PreviewContainer(
            parent_doc_id=parent_id,
            query_signature=self._app._search.query_signature(),
            total_chunks=len(chunks),
        )
        pane.mount(container)
        self.activate_container(container)
        if not chunks:
            return
        first_chunk = chunks[0]
        title = Static(Path(first_chunk.path).name, classes="preview-title")
        container.mount(title)
        for i, c in enumerate(chunks):
            if uses_markdown_renderer(c):
                self._mount_structured_chunk(container, c)
            else:
                self._mount_plain_chunk(container, c)
            container.mounted_indices.add(i)

    def _mount_plain_chunk(
        self,
        parent: Container | VerticalScroll,
        c: FileChunk,
        *,
        before: Widget | None = None,
    ) -> None:
        """Per-line layout for non-markdown chunks. Each body line becomes
        its own Static so ``scroll_to_widget`` can target the first matched
        line, and the match-row gets a subtle accent overlay.

        We deliberately don't mount the locator header (``p. 351 · ...``)
        — that information lives on the sidebar result row; repeating it
        per-chunk in the preview is just visual clutter. The first body
        widget of each chunk gets a ``chunk-first`` class so a small top
        gap still marks the chunk boundary.

        ``before`` (if supplied) makes every widget mount immediately
        before that anchor — used by background-fill prepending so
        chunks land in document order even when mounted out of sequence.
        """
        _, pieces = render_chunk_pieces(
            c, query=self._app._search.current_query, match_spec=self._app._effective_match_spec
        )
        first_widget: Static | None = None
        first_match: Static | None = None
        for line_text, has_match in pieces:
            line_w = Static(line_text, classes="chunk-line")
            line_w.fnd_text = line_text  # type: ignore[attr-defined]
            if has_match:
                line_w.add_class("chunk-line-match")
            parent.mount(line_w, before=before)
            if first_widget is None:
                line_w.add_class("chunk-first")
                first_widget = line_w
            if has_match and first_match is None:
                first_match = line_w
        if first_widget is None:
            return
        # Write to the owning container only. The app-level alias dicts get
        # refreshed by _activate_preview_container so writing here would
        # corrupt whichever container is currently active (esp. during
        # concurrent prefetch on a different file).
        if isinstance(parent, PreviewContainer):
            parent.chunk_widgets[c.chunk_seq] = first_widget
            parent.match_targets[c.chunk_seq] = first_match or first_widget
        else:
            self.chunk_widgets[c.chunk_seq] = first_widget
            self.match_targets[c.chunk_seq] = first_match or first_widget

    def _mount_structured_chunk(
        self,
        parent: Container | VerticalScroll,
        c: FileChunk,
        *,
        before: Widget | None = None,
    ) -> None:
        """Structural markdown rendering for formats whose extractor
        populated ``body_md`` (md / docx / pptx).

        Mounts a single :class:`FNDMarkdown` widget per chunk —
        Textual builds out the per-block widget tree (headings,
        paragraphs, tables, fenced code, lists, blockquotes) and our
        highlight-aware subclasses overlay match-only spans on the
        rendered Content. Code fences (``FNDMarkdownFence``) keep the
        Rich syntax highlighting and add the match overlay on top, so
        query terms inside a code block are highlighted too.

        ``_chunk_widgets`` maps the chunk seq to the FNDMarkdown
        widget itself (used for chunk-boundary scrolling); ``_match_
        targets`` maps to ``first_match_block`` when the chunk has
        matches, falling back to the FNDMarkdown so scroll still
        lands at the chunk top when nothing matched.
        """
        source = c.body_md or _legacy_blocks_to_md(c.blocks)
        import os

        if os.environ.get("_FND_W_HYBRID") == "1":
            from fnd.tui._md_hybrid import FNDChunkHybrid

            try:
                pane_widget = self._app.query_one("#preview_pane", VerticalScroll)
                wrap_width = max(20, pane_widget.content_size.width - 1)
            except Exception:
                wrap_width = 80
            md_widget = FNDChunkHybrid(
                source,
                match_spec=self._app._effective_match_spec,
                wrap_width=wrap_width,
                classes="chunk-section chunk-md-body chunk-first",
            )
        else:
            md_widget = FNDMarkdown(
                source,
                match_spec=self._app._effective_match_spec,
                # Default-on: honour the model default when no config is injected.
                render_mermaid=(
                    self._app._config.defaults.render_mermaid if self._app._config else True
                ),
                classes="chunk-section chunk-md-body chunk-first",
            )
        parent.mount(md_widget, before=before)
        # See _mount_plain_chunk for why we write only to the owning
        # container (concurrent prefetch on a different file would
        # otherwise overwrite the active container's dict).
        if isinstance(parent, PreviewContainer):
            parent.chunk_widgets[c.chunk_seq] = md_widget
            parent.match_targets[c.chunk_seq] = md_widget
        else:
            self.chunk_widgets[c.chunk_seq] = md_widget
            self.match_targets[c.chunk_seq] = md_widget

    def rerender_current(self) -> None:
        """Drop the preview cache (its widgets carry already-applied
        highlight spans, so they can't simply re-paint themselves) and
        re-issue the render for the focused result. Used by
        ``action_toggle_highlights`` so the new overlay state lands
        without waiting for the user to move the cursor."""
        import contextlib

        # Re-use the per-query cache invalidation: clear decoded
        # chunks, kill any in-flight mount worker, drop cached
        # PreviewContainers from the DOM, reset alias maps.
        # Invalidate first so a mid-flight mount's finally drops its stale
        # container instead of re-caching it after the clear below.
        self.bump_reset_generation()
        self.chunk_cache.clear()
        self.prebuilt_cache.clear()
        self.cancel_mount_task()
        self._app._lazy.cancel()
        evicted = self.preview_cache.clear()
        for old in evicted:
            with contextlib.suppress(Exception):
                old.remove()
        if self.active is not None and self.active.parent is not None:
            with contextlib.suppress(Exception):
                self.active.remove()
        self.active = None
        self._app._flat.cache.clear()
        self._app._flat.reset()
        self.chunk_widgets = {}
        self.match_targets = {}
        self.parent_id = None
        self.hide_progress_bar()
        # Re-trigger the preview render for the focused result. We pull
        # the (parent_id, focus_chunk_seq) pair off the cursor's
        # data — same logic ``_on_tree_highlight`` uses on cursor
        # change.
        try:
            tree = self._app.query_one("#results_pane", Tree)
        except Exception:
            return
        cursor = tree.cursor_node
        if cursor is None or not isinstance(cursor.data, dict):
            return
        kind = cursor.data.get("kind")
        if kind == "section":
            hit: Hit = cursor.data["hit"]
            self.render_full_doc(hit.parent_id, focus_chunk_seq=hit.chunk_seq)
        elif kind == "file":
            g: FileGroup = cursor.data["group"]
            top = g.hits[0] if g.hits else None
            self.render_full_doc(g.parent_id, focus_chunk_seq=top.chunk_seq if top else 0)

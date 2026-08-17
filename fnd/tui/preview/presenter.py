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
from fnd.tui.preview import frozen_store, tuning
from fnd.tui.preview.coverage import coverage_targets, filler_targets, neighbour_order
from fnd.tui.preview.frozen import (
    FrozenChunk,
    FrozenChunkView,
    FrozenDocument,
    FrozenDocumentView,
)
from fnd.tui.preview.frozen_store import ChunkCaptureStore, FrozenDocumentStore
from fnd.tui.preview.liveness import is_condemned, is_live
from fnd.tui.preview.warm_host import WarmHost
from fnd.tui.preview_dispatcher import choose_preview_mode, uses_markdown_renderer
from fnd.tui.preview_scroll import ScrollAnchor
from fnd.tui.preview_scrollbar import MatchAwareScroll
from fnd.tui.widgets.markdown import FNDMarkdown, _legacy_blocks_to_md
from fnd.tui.widgets.preview_container import PreviewCache, PreviewContainer

if TYPE_CHECKING:
    from textual.geometry import Region

    from fnd.query import FileChunk, FileGroup, Hit
    from fnd.tui.app import FNDApp
    from fnd.tui.line_buffer import RenderedDocument

__all__ = ["PreviewPresenter", "target_from_node_data"]


def target_from_node_data(data: Any) -> tuple[str, int] | None:
    """Map a results-tree node's ``data`` to the ``(parent_id, focus_chunk_seq)``
    the preview should be showing for it, or None if it isn't a result row.

    The single definition of that mapping. It used to be written out three
    times — here, in :meth:`PreviewPresenter.rerender_current`, and in
    ``FNDApp._load_result_node`` — with the loader deciding what to mount
    through one copy and the settle-time paint check deciding what *should* be
    mounted through another. A divergence between the copies (a new node
    ``kind``, a different fallback for an empty ``hits`` list) would have them
    disagree about which file the cursor is on: the check would then either
    repair a target the loader never paints, or excuse a real divergence. That
    is the failure class this module exists to close, so the mapping gets one
    home."""
    if not isinstance(data, dict):
        return None
    kind = data.get("kind")
    if kind == "section":
        hit: Hit = data["hit"]
        return (hit.parent_id, hit.chunk_seq)
    if kind == "file":
        g: FileGroup = data["group"]
        top = g.hits[0] if g.hits else None
        return (g.parent_id, top.chunk_seq if top else 0)
    return None


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
        # Captured whole-file documents, harvested from the freeze sweep, plus
        # the single reused widget that shows them. One widget for the session
        # rather than one per file: remounting per navigation is exactly the DOM
        # churn this substrate exists to avoid.
        self.document_store = FrozenDocumentStore()
        self.document_view: FrozenDocumentView | None = None
        # Strong ref so the loop does not GC the in-flight coverage task.
        self._coverage_task: asyncio.Task[None] | None = None
        # Cursor position the running coverage plan was built for.
        self._coverage_anchor: tuple[str, int] | None = None
        # Per-chunk captures, filled by coverage and read by every mount path.
        # Separate from the document store because this one is SPARSE: it holds
        # the matches scattered through a file, which a contiguous document
        # cannot express. See fnd/tui/preview/coverage.py.
        self.capture_store = ChunkCaptureStore()
        # Pane width the ON-SCREEN document was installed at, so a resize can
        # tell a stale view from one that is still painting at the right width.
        self._document_view_width: int = 0
        # Strong ref so the loop doesn't GC the in-flight warm task, plus the
        # file it is growing so a re-harvest of the same file doesn't restart it.
        self._warm_task: asyncio.Task[None] | None = None
        self._warm_bootstrap: asyncio.Task[None] | None = None
        self._warm_parent: str | None = None
        # Chunks are built and captured here rather than in the visible
        # container — see warm_host for why every way of hiding a live container
        # is either uncapturable or silently blank.
        self._warm_host = WarmHost(app)
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
        # The in-flight chunk-decode worker, if any. A cold load cancels the
        # mount task and the debounce timer before handing off to this worker,
        # so without tracking it the pipeline looks idle for the whole decode —
        # precisely while the pane is still (deliberately) showing the previous
        # file. See pipeline_busy.
        self.decode_worker: object | None = None
        # Prebuilt flat-buffer bundles keyed by (parent_id, query_sig).
        # Cleared on query change — highlight spans are baked in at build time.
        self.prebuilt_cache: dict[tuple[str, str], RenderedDocument] = {}
        # Debounced preview load — latest target + Timer.
        from typing import Any as _Any

        self.load_timer: _Any | None = None
        self.load_target: tuple[str, int] | None = None
        # True while the cursor is moving via Option/Alt + arrow ("scan" mode):
        # schedule_load skips its leading fire so fast browsing doesn't mount
        # every row — the preview only loads once the sweep settles (trailing).
        # Set by the app's alt-arrow handler, cleared on a load or a plain move.
        self._scan_move: bool = False
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
        # Bounded-time reveal backstop timer (see _arm_reveal_watchdog). Re-armed
        # on every pre-reveal activation; disarmed when the container is revealed.
        self._reveal_watchdog: object | None = None
        # Settle-time paint check (see _arm_paint_check): armed on every
        # navigation, verifies the pane really is showing the cursor's file.
        self._paint_check: object | None = None
        self._paint_check_rearms: int = 0
        # The target a repair has already been spent on, so a repair that itself
        # fails cannot cascade into a re-dispatch storm.
        self._paint_repair_target: tuple[str, int] | None = None

    def _arm_reveal_watchdog(self) -> None:
        """(Re)start the bounded-time reveal backstop for the active container.

        The active container mounts invisible (``-pre-reveal``) and is revealed by
        its finalize task once the layout settles. If that task is cancelled
        before it reveals, or hangs awaiting chunks a cancelled mount never
        mounted, the container would stay invisible. This timer guarantees the
        invariant "an active container becomes visible within a bounded time":
        when it fires, :meth:`reveal_active` reveals the still-invisible active
        container. A fast finalize reveals first and calls :meth:`reveal` which
        disarms this, so the common path never hits the timer (no flash)."""
        self._cancel_reveal_watchdog()
        self._reveal_watchdog = self._app.set_timer(
            tuning.REVEAL_WATCHDOG_MS / 1000.0,
            self.reveal_active,
            name="preview-reveal-watchdog",
        )

    def _cancel_reveal_watchdog(self) -> None:
        if self._reveal_watchdog is not None:
            with contextlib.suppress(Exception):
                self._reveal_watchdog.stop()  # type: ignore[attr-defined]
            self._reveal_watchdog = None

    # ── settle-time paint check ────────────────────────────────────

    def is_painted(self) -> bool:
        """Is the preview pane actually showing a document right now?

        The outcome the whole pipeline exists to produce: some widget that is
        live in the DOM, not hidden, not still invisible behind ``-pre-reveal``,
        and carrying displayed content."""
        # The document substrate sets BOTH `active` and `active_buffer` to None,
        # so without this a served document reads as an unpainted pane and the
        # repair below rebuilds a preview that is on screen and correct —
        # measured at 19 spurious rebuilds in a 14-navigation run, each one
        # clearing the cache and re-dispatching.
        view = self.active_document_view()
        if view is not None:
            # Ask the substrate what it is actually painting. `bool(chunks)`
            # cannot fail while a document object exists, so it silently
            # disabled this rescue — measured, a view scrolled past its own
            # strips paints 0 of 25 rows and still reported healthy. render_line
            # is the line API, so this costs one terminal height, not a
            # document.
            if not is_live(view) or view.size.height <= 0:
                return False
            return any(view.render_line(y).text.strip() for y in range(view.size.height))
        buf = self._app._flat.active_buffer
        if buf is not None:
            return is_live(buf) and not buf.has_class("-hidden")
        container = self.active
        if container is None or not is_live(container):
            return False
        if container.has_class("-hidden") or container.has_class("-pre-reveal"):
            return False
        return any(w.display for w in container.children)

    def showing_parent(self) -> str | None:
        """The parent_doc_id of whatever the pane is currently showing."""
        # Same blindness as is_painted: a served document has no container and
        # no buffer, so the check compared None against the cursor's file and
        # concluded the pane was showing the wrong thing.
        if self.active_document_view() is not None:
            return self.parent_id
        buf = self._app._flat.active_buffer
        if buf is not None:
            pid = getattr(buf, "parent_doc_id", None)
            return pid if isinstance(pid, str) else None
        container = self.active
        return container.parent_doc_id if container is not None else None

    def pipeline_busy(self) -> bool:
        """True while a decode / load / mount / finalize for the preview is
        still in flight — i.e. the pane not yet showing the cursor's file is
        expected right now, not a strand."""
        if self.load_timer is not None:
            return True
        # The decode is the easiest one to miss: a cold load cancels the mount
        # task and the debounce timer BEFORE handing the file to this worker, so
        # all the other signals read idle for its whole duration — while the pane
        # is still deliberately showing the previous file. Left untracked, the
        # check would call that a strand and spend its one repair, and the repair
        # re-enters render_full_doc, whose worker group is exclusive — restarting
        # the very decode that was about to finish.
        worker = self.decode_worker
        if worker is not None:
            with contextlib.suppress(Exception):
                if not worker.is_finished:  # type: ignore[attr-defined]
                    return True
        task = self.mount_task
        if task is not None:
            with contextlib.suppress(Exception):
                if not task.done():  # type: ignore[attr-defined]
                    return True
        container = self.active
        finalize = getattr(container, "_finalize_task", None) if container is not None else None
        if finalize is not None:
            with contextlib.suppress(Exception):
                if not finalize.done():
                    return True
        return False

    def cursor_target(self) -> tuple[str, int] | None:
        """The (parent_id, focus_chunk_seq) the results cursor currently points
        at — the file the preview is supposed to be showing."""
        try:
            tree = self._app.query_one("#results_pane", Tree)
        except Exception:
            return None
        cursor = tree.cursor_node
        return target_from_node_data(cursor.data) if cursor is not None else None

    def _cancel_paint_check(self) -> None:
        if self._paint_check is not None:
            with contextlib.suppress(Exception):
                self._paint_check.stop()  # type: ignore[attr-defined]
            self._paint_check = None

    def _arm_paint_check(self) -> None:
        """Arm the settle-time invariant check.

        Prevention at the consumption seams is the real fix (see
        ``fnd/tui/preview/liveness.py``); this only ensures that if any seam
        ever regresses, the user sees one extra rebuild instead of a preview
        that stays wrong until they navigate away and come back."""
        self._cancel_paint_check()
        self._paint_check_rearms = 0
        self._paint_check = self._app.set_timer(
            tuning.PAINT_CHECK_MS / 1000.0,
            self._verify_painted,
            name="preview-paint-check",
        )

    def _verify_painted(self) -> None:
        """The invariant, checked against the CURSOR rather than against
        whichever navigation happened to arm this timer.

        Deliberately not "did the navigation I was armed for land?": a stale
        dispatch (a late cursor echo, a debounce timer firing for a row the user
        has already left) re-arms this check under its own target, so keying off
        the armed target would make the check excuse the very divergence it
        exists to catch. The question is always the same one the user is asking:
        *is the pane showing the file I have selected?*"""
        self._paint_check = None
        # Option/Alt scan mode moves the cursor WITHOUT loading, by design —
        # a divergence here is the feature working, not a strand.
        if self._scan_move:
            return
        target = self.cursor_target()
        if target is None:
            return  # no selection (empty results, non-result node) — nothing owed
        # Still legitimately working (a big file can mount for a while) — look
        # again rather than pre-empting a healthy navigation. Bounded, so a
        # wedged pipeline still gets its one repair.
        if self.pipeline_busy() and self._paint_check_rearms < tuning.PAINT_CHECK_MAX_REARMS:
            self._paint_check_rearms += 1
            self._paint_check = self._app.set_timer(
                tuning.PAINT_CHECK_MS / 1000.0,
                self._verify_painted,
                name="preview-paint-check",
            )
            return
        if self.is_painted() and self.showing_parent() == target[0]:
            self._paint_repair_target = None
            return
        if self._paint_repair_target == target:
            # One repair per target. An earlier recovery attempt in this
            # subsystem re-dispatched on every failed reveal and produced a
            # cascade that was far worse than the strand; this cap is what
            # keeps a failing repair from doing that again.
            self.diag_log(f"paint check unresolved parent={target[0][:8]} — repair spent")
            return
        self._paint_repair_target = target
        self.diag_log(
            f"paint check FAILED cursor={target[0][:8]}/{target[1]} "
            f"showing={str(self.showing_parent())[:8]} painted={self.is_painted()} — rebuilding"
        )
        parent_id, focus_chunk_seq = target
        # Drop whatever half-state we are looking at so the rebuild starts clean
        # (a dead container in the cache would just be served straight back).
        with contextlib.suppress(Exception):
            evicted = self.preview_cache.clear()
            for old in evicted:
                with contextlib.suppress(Exception):
                    old.remove()
        self.active = None
        self.render_full_doc(parent_id, focus_chunk_seq=focus_chunk_seq)

    def schedule_load(self, parent_id: str, focus_chunk_seq: int) -> None:
        """Debounce a cursor-move → preview-load; coalesces rapid arrow sweeps."""
        if self._scan_move:
            # Option/Alt+arrow scan: browse the results without mounting anything.
            # Record where the cursor is but DON'T load and DON'T arm a timer — so
            # even pausing mid-sweep never mounts. The preview loads only when the
            # user presses a normal key (the app clears _scan_move, and the next
            # schedule_load takes the leading-edge path below). Release detection
            # isn't available in the terminal, so "load on a normal key" is the
            # portable stand-in for "load when Option is released".
            #
            # Cancel any cooldown timer a *prior* normal nav left armed: a scan
            # started within that window would otherwise have the old timer fire
            # and mount the scanned row on pause, defeating scan mode.
            if self.load_timer is not None:
                with contextlib.suppress(Exception):
                    self.load_timer.stop()
                self.load_timer = None
            self.load_target = (parent_id, focus_chunk_seq)
            return
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
        if self.load_timer is None:
            # Leading edge: the cursor was settled, so load NOW — a deliberate
            # single jump shouldn't wait out the window (that wait was ~150ms of
            # every nav's perceived lag). Then open a coalescing window so a rapid
            # arrow-sweep that follows only loads its FINAL row (the trailing
            # fire), not every row. The inflight-target dedup in fire_pending_load
            # keeps a same-tick park+dispatch pair (same target) from double-firing.
            self.fire_pending_load()
        else:
            # Mid-sweep: a load is already coalescing. Restart the window so the
            # trailing fire lands once the cursor finally settles.
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
        # A new query / reset also ends any scan sweep, so the post-query cursor
        # park (and the next deliberate move) load normally instead of being
        # silently suppressed as a scan move.
        self._scan_move = False

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
        # Re-plan what to capture ahead around the NEW cursor position. Driven
        # from here rather than from the mount because most navigations never
        # reach a mount — a target already on screen returns early — and those
        # are precisely the moments with time to spare for capturing ahead.
        self.start_coverage(parent_id, focus_chunk_seq)
        # Every navigation is checked once it should have settled (see
        # _arm_paint_check) — the single place that verifies the OUTCOME rather
        # than one mechanism, so no seam can strand the pane indefinitely.
        self._arm_paint_check()
        # Refresh the preview's border indicator for the NEW target. Arming is
        # the one event common to every navigation; the indicator was otherwise
        # driven only by a document mount (``refresh_match_scrollbar`` →
        # ``MatchNavigator.rebuild``) and by scroll events, so stepping between
        # section rows of the SAME file refreshed nothing — and a row whose
        # chunk has no match doesn't even scroll, so the border kept displaying
        # the previous row's state.
        self._app.call_after_refresh(self._app._refresh_preview_match_indicator)

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
        self.decode_worker = self._app.run_worker(
            _load, thread=True, exclusive=True, group="preview-load"
        )

    def sweep_stranded_containers(self, *, keep: PreviewContainer | None = None) -> int:
        """Remove PreviewContainers that nothing owns any more; returns the count.

        Stranded = not in the widget cache, not ``keep``, and not still being
        filled by a prefetch task (removing one of those orphans the task and
        trips a MountError on its next mount-before call).

        Must run on EVERY navigation, not only cross-file ones. The same-file
        out-of-window path builds a fresh container and returns early, and used
        to rely on "the old container is swept on the next navigation" — which
        never happened while the user stayed inside one file, the common case in
        a large document. Measured over 30 in-file navigations: 23 containers,
        270 mounted chunks and 14,551 widgets left in the pane, with navigation
        degrading from ~1.9s to 4-7.8s and the scroll committing more than once
        per navigation as the DOM grew.
        """
        import contextlib as _contextlib

        cached = set(self.preview_cache._cache.values())
        removed = 0
        for stranded in list(self._app.query(PreviewContainer)):
            # ``outgoing`` is the previous file, deliberately held on screen so
            # the incoming one can build behind it and swap in without a blank
            # frame. It is neither active nor cached, so it looks exactly like a
            # strand — removing it mid-swap would blank the pane for the whole
            # build.
            if (
                stranded in cached
                or stranded is keep
                or stranded is self.active
                or stranded is self.outgoing
            ):
                continue
            pfetch = getattr(stranded, "_prefetch_task", None)
            if pfetch is not None and not pfetch.done():
                continue
            with _contextlib.suppress(Exception):
                stranded.remove()
                removed += 1
        if removed:
            self.diag_log(f"sweep removed={removed} containers")
        return removed

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
        import os as _os_freeze
        import time as _time

        from fnd.tui import _perf
        from fnd.tui.preview.frozen import FrozenChunkView, freeze

        freeze_on_prune = _os_freeze.environ.get("_FND_FREEZE_ON_PRUNE") == "1"

        _pt0 = _time.perf_counter()
        frozen_count = 0
        self.begin_reconcile_scroll()
        try:
            for i, w in to_remove:
                seq = chunks[i].chunk_seq
                # Freeze rather than discard where we can. Removing the chunk
                # frees its widgets but throws away the render, so scrolling back
                # to it rebuilds from markdown; a frozen stand-in keeps what it
                # painted for one widget instead of ~42, and — because it is
                # given the exact height the tree occupied — swapping it in moves
                # nothing on screen, so it needs none of the scroll compensation
                # below.
                captured = freeze(w, seq) if freeze_on_prune else None
                if captured is not None:
                    view = FrozenChunkView(captured)
                    try:
                        w.parent.mount(view, before=w)  # type: ignore[union-attr]
                    except Exception:
                        captured = None
                    else:
                        above_height -= captured.height if i < keep_lo else 0
                        container.chunk_widgets[seq] = view
                        container.match_targets.pop(seq, None)
                        frozen_count += 1
                # display:none leaves the arrange immediately; remove() then frees
                # it. (Keeping ~1000s of display:none widgets alive is worse — they
                # still get walked by settle and inflate the next mount.)
                with contextlib.suppress(Exception):
                    w.display = False
                with contextlib.suppress(Exception):
                    w.remove()
                if captured is None:
                    container.mounted_indices.discard(i)
                    container.chunk_widgets.pop(seq, None)
                    container.match_targets.pop(seq, None)
            if above_height > 0:
                with contextlib.suppress(Exception):
                    pane.scroll_to(y=max(0.0, vtop - above_height), animate=False, immediate=True)
                    self.diag_log(
                        f"scroll site=prune y={max(0.0, vtop - above_height):.0f} "
                        f"removed={len(to_remove)}"
                    )
        finally:
            self.end_reconcile_scroll()
        _perf.mark(
            "prune",
            removed=len(to_remove),
            frozen=frozen_count,
            ms=(_time.perf_counter() - _pt0) * 1000.0,
        )
        if frozen_count:
            self.diag_log(f"prune froze={frozen_count} removed={len(to_remove) - frozen_count}")

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
            # NOT hidden here: the flat buffer replaces it in the same tick, and
            # hiding first leaves the pane blank in between.
            self.dispatch_flat_mount(parent_id, focus_chunk_seq, chunks, prebuilt=prebuilt)
            self.hide_document_view()
            return

        query_sig = self._app._search.query_signature()

        # Already captured this file at this width: serve the document and skip
        # the build entirely. Checked before the same-file fast paths below
        # because it beats all of them — there is nothing to settle or retry.
        if self.document_preview_enabled():
            try:
                width = self._app.query_one("#preview_pane", VerticalScroll).content_size.width
            except Exception:
                width = 0
            doc = self.document_store.get(parent_id, query_sig, width) if width > 0 else None
            # Only serve when the target is INSIDE the captured run. A document
            # is a self-consistent slice, not necessarily the whole file, so a
            # jump past its end has to rebuild rather than land nowhere.
            if (
                doc is not None
                and doc.row_of_chunk(focus_chunk_seq) is not None
                and self.dispatch_document_mount(parent_id, focus_chunk_seq, doc)
            ):
                return
        # Deliberately NOT hidden here. A served document is the OUTGOING
        # substrate for the structural build that follows, and that build takes
        # 1-2s. Hiding it now leaves the pane blank for the whole of it —
        # measured at 45 of 150 samples blank, against 0 of 150 with the
        # substrate off. It is hidden instead when the replacement is revealed
        # (see `reveal`), which is how the widget path has always avoided this.

        # Same file + same query already active. Two sub-cases:
        #   (a) target chunk widget exists — just scroll, no remount.
        #   (b) target chunk not yet mounted (still loading the file
        #       and the user clicked a result above the load front):
        #       cancel the in-flight task and resume the SAME container
        #       with the new focus window. We keep all already-mounted
        #       chunks; the worker only mounts the missing ones.
        if (
            self.active is not None
            and is_live(self.active)
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
            # match in one tick.
            self.cancel_mount_task()
            self._app._lazy.cancel()
            self.hide_progress_bar()
            # Sweep and prune BEFORE building the replacement. This branch used
            # to do neither, on the assumption that the old container would be
            # "swept on the next navigation" — but the sweep lives on the
            # cross-file path below, which this early return never reaches. Stay
            # inside one file (the common case in a large document) and nothing
            # ever reclaimed anything: 30 navigations left 23 containers, 270
            # mounted chunks and 14,551 widgets in the pane, with navigation
            # degrading from ~1.9s to 4-7.8s as Textual's arrange scaled with it.
            self.prune_active_to_window()
            self.sweep_stranded_containers()
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

        cached_containers = set(self.preview_cache._cache.values())
        # Resolve the container we intend to reuse BEFORE sweeping. The sweep
        # and the DOM-scan adopt used to run in the other order, and because
        # ``remove()`` is deferred (the widget keeps a parent and stays in
        # ``query()`` until its Prune is processed) the adopt happily picked up
        # a container the sweep had just condemned. The mount then saw a parent,
        # skipped its ``pane.mount()``, built into it and activated it — and the
        # queued Prune detached it: ``active`` left pointing outside the tree,
        # pane blank, nothing to heal it. Choosing first, then sweeping
        # everything except the choice, makes that self-condemnation impossible.
        cached = self.preview_cache.get(parent_id, query_sig)
        # A cache entry that is no longer LIVE is not a valid hit — that covers
        # both an already-detached widget and one whose removal is merely queued
        # (see fnd/tui/preview/liveness.py). Purge it and rebuild fresh.
        if cached is not None and not is_live(cached):
            key = (cached.parent_doc_id, cached.query_signature)
            if self.preview_cache._cache.get(key) is cached:
                del self.preview_cache._cache[key]
            cached_containers.discard(cached)
            # Don't leave self.active dangling on the purged container through
            # the async rebuild — a fresh one is coming.
            if self.active is cached:
                self.active = None
            self.diag_log(f"cache hit not live parent={parent_id[:8]} — rebuilding fresh")
            cached = None
        if cached is None:
            for c in self._app.query(PreviewContainer):
                # Never adopt a PREFETCH-origin container (one that has a
                # ``_prefetch_task``, live or done). Prefetch re-anchors on the
                # cursor and continually mounts/evicts/removes its own containers
                # for the hot file; adopting one for a user mount races that
                # removal — the adopted widget is detached mid-mount and the
                # finalize reveals a zero-region ghost (blank pane). Build a
                # fresh, user-owned container instead, which prefetch won't touch
                # (and eviction can't drop while it is the protected active one).
                if getattr(c, "_prefetch_task", None) is not None:
                    continue
                # Nor one that is already being torn down.
                if not is_live(c):
                    continue
                if (
                    c.parent_doc_id == parent_id
                    and c.query_signature == query_sig
                    and c not in cached_containers
                ):
                    cached = c
                    break

        self.sweep_stranded_containers(keep=cached)
        if (
            self.active is not None
            and self.active is not cached
            and self.active not in cached_containers
        ):
            self.active = None

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
        # Stop the structural pipeline we are replacing — the symmetric step the
        # structural branch of dispatch_mount does for itself. Without it an
        # in-flight structural mount kept building and, on completion,
        # ``activate_container`` hid every LineBufferPreview and showed its own
        # container: the preview displayed the file the user had just navigated
        # away from. ``schedule_load``'s navigate-away cancel doesn't cover this
        # — it compares against ``self.active``, which the flat path sets to
        # None, so with a flat preview on screen there is nothing to compare.
        self.cancel_mount_task()
        self._app._lazy.cancel()
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
        if buf is None:
            # No pane to mount into: the screen went away while this load
            # was in flight. Nothing to show and nothing to fix.
            return
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
        # A container activated invisibly is on the clock: arm the bounded-time
        # reveal backstop so a cancelled/hung finalize can't leave it stranded.
        # A visible (non-pre-reveal) activation cancels any pending watchdog.
        if pre_reveal:
            self._arm_reveal_watchdog()
        else:
            self._cancel_reveal_watchdog()
        # Cache-hit paths return without _mount_chunks_async (which is
        # where _refresh_status normally fires at the end); refresh here
        # so the pane title swaps to the activated file immediately.
        self._app._refresh_status()

    # ── StructuralHost / FlatHost implementation ───────────────────
    # The scroll strategies read the pane, chunk/match maps, match spec
    # and reveal/reconcile gates through these.

    def preview_pane(self) -> VerticalScroll:
        return self._app.query_one("#preview_pane", VerticalScroll)

    async def _reattach_active_if_detached(self, container: PreviewContainer) -> None:
        """Re-mount ``container`` if it is still the active preview but a
        concurrent prefetch/eviction race detached it from the DOM mid-mount.

        The resume path can adopt a prefetch-mounted container that prefetch
        churn then removes, and the single-slot cache can evict a container out
        from under an in-flight mount. Either leaves ``self.active`` pointing at a
        detached widget; revealing it would surface a blank pane. The widget's
        chunk tree travels with it, so re-mounting the SAME instance restores the
        built content. Called just before the finalize reveals, so the reveal
        always lands on an attached container. A no-op unless the container is
        both the active preview and detached."""
        if container is not self.active or is_live(container):
            return
        if is_condemned(container):
            # Its message loop is already closing — ``pane.mount()`` cannot
            # revive it, and the queued Prune would tear it out again. Drop the
            # active reference instead so the reveal no-ops and the settle-time
            # paint check (:meth:`_verify_painted`) rebuilds from scratch.
            self.diag_log(
                f"finalize re-attach SKIPPED (condemned) parent={container.parent_doc_id[:8]}"
            )
            self.active = None
            return
        try:
            await self.preview_pane().mount(container)
        except Exception as exc:
            # Teardown race (app quitting) or an un-remountable widget: the reveal
            # then no-ops on the still-detached container. Log the real outcome.
            self.diag_log(f"finalize re-attach FAILED parent={container.parent_doc_id[:8]}: {exc}")
            return
        self.diag_log(
            f"finalize re-attach parent={container.parent_doc_id[:8]} (was detached mid-mount)"
        )

    def effective_match_spec(self) -> MatchSpec:
        return self._app._effective_match_spec

    def diag_log(self, msg: str) -> None:
        self._app._diag_log(msg)

    def above_window_pending(self, focus_chunk_seq: int) -> bool:
        """Is content still to arrive ABOVE the focus chunk?

        True while any chunk of the mount window above the focus chunk is either
        not mounted yet or still building. Both matter, and the first is the one
        that is easy to miss: navigating backwards into a file mounts that window
        *after* the scroll is first attempted, so a check over the widgets that
        happen to be mounted sees nothing pending and commits a scroll the
        arriving content then pushes down.

        This is the same guarantee ``_finalize_via_lock`` gets from its
        ``expected_above_seqs``; the difference is only that the scroll strategy
        can't know the window, so it asks the presenter, which does.
        """
        container = self.active
        if container is None:
            return False
        chunks = self.chunk_cache.get(container.parent_doc_id)
        if not chunks:
            return False
        focus_idx = next((i for i, c in enumerate(chunks) if c.chunk_seq == focus_chunk_seq), None)
        if focus_idx is None:
            return False
        for i in range(max(0, focus_idx - tuning.VISIBLE_FIRST_ABOVE), focus_idx):
            widget = container.chunk_widgets.get(chunks[i].chunk_seq)
            if widget is None:
                return True
            build_done = getattr(widget, "build_done", None)
            if build_done is not None and not build_done.is_set():
                return True
        return False

    def call_after_refresh(self, callback: Callable[..., Any], *args: Any, **kwargs: Any) -> object:
        return self._app.call_after_refresh(callback, *args, **kwargs)

    def active_flat_buffer(self) -> LineBufferPreview | None:
        return self._app._flat.active_buffer

    def active_document_view(self) -> FrozenDocumentView | None:
        """The frozen-document preview when one is on screen AND still valid.

        Checked lazily rather than trusting a resize event to have fired: the
        resize hook runs before layout and so reads the pre-resize width. Here
        the width is whatever the pane reports at the moment of asking, which
        makes a stale document impossible to scroll or serve regardless of what
        the event loop did.
        """
        view = self.document_view
        if view is None or view.has_class("-hidden"):
            return None
        try:
            width = self._app.query_one("#preview_pane", VerticalScroll).content_size.width
        except Exception:
            return None
        if width > 0 and self._document_view_width not in (0, width):
            return None
        return view

    def document_preview_enabled(self) -> bool:
        """Serve captured documents instead of rebuilding chunk widgets.

        Off by default: the substrate is complete but has not yet had the
        hand-driven session on a real corpus that every other default flip on
        this branch got. ``_FND_DOC_PREVIEW=1`` opts in.
        """
        import os as _os

        return _os.environ.get("_FND_DOC_PREVIEW") == "1"

    def dispatch_document_mount(
        self,
        parent_id: str,
        focus_chunk_seq: int,
        doc: FrozenDocument,
    ) -> bool:
        """Show a captured document: one widget, one synchronous scroll.

        No build, no settle, no retry chain — the target row is known the moment
        the document exists, which is the entire reason for the substrate.
        Returns False when there is no pane to mount into (screen torn down).
        """
        from fnd.tui.line_buffer import LineBufferPreview
        from fnd.tui.widgets.preview_container import PreviewContainer

        self.cancel_mount_task()
        self._app._lazy.cancel()
        try:
            pane = self._app.query_one("#preview_pane", VerticalScroll)
        except Exception:
            return False

        view = self.document_view
        if not is_live(view):
            view = FrozenDocumentView(doc, id="preview_document")
            self.document_view = view
            pane.mount(view)
        else:
            assert view is not None
            view.set_document(doc)

        self._document_view_width = pane.content_size.width
        # The document view is itself a ScrollView filling the pane and carrying
        # the match markers, so the pane's own bar would be a second, inert
        # position indicator beside the real one.
        pane.add_class("-document")
        self.clear_pane_placeholder()
        for child in self._app.query(PreviewContainer):
            child.add_class("-hidden")
        for child in self._app.query(LineBufferPreview):
            child.add_class("-hidden")
        # Painted immediately. An earlier version held the view unpainted until
        # its scroll landed, to avoid one frame at the wrong offset — and
        # stranded the pane BLANK whenever the reveal condition never arrived,
        # with the document's scrollbar markers still painting beside an empty
        # pane (opacity hides content, not scrollbars). A frame at the wrong
        # offset is a glitch; an indefinitely blank preview is a broken app, and
        # the structural path's reveal machinery exists precisely because this
        # is hard to get right.
        view.remove_class("-pre-reveal")
        view.remove_class("-hidden")
        self._app._flat.active_buffer = None
        self.active = None
        # Straggler scrolls must not find a stale per-chunk widget to aim at.
        self.chunk_widgets = {}
        self.match_targets = {}

        self._app._preview_scroll.arm(ScrollAnchor(parent_id, focus_chunk_seq))
        self._app._preview_scroll.reconcile()
        self.diag_log(
            f"dispatch_document parent={parent_id[:8]} chunks={len(doc.chunks)} "
            f"rows={doc.total_rows} width={doc.width}"
        )
        self.hide_progress_bar()
        self.parent_id = parent_id
        self._app._refresh_status()
        self._warm_served_document(parent_id, doc, pane.content_size.width)
        return True

    def _warm_served_document(self, parent_id: str, doc: FrozenDocument, width: int) -> None:
        """Keep growing a document that was served from the store.

        Without this, warming only ran off the back of a widget-path mount, so
        every later visit — served from the store, returning before harvest —
        never grew. The chunk list is fetched on a worker because decoding a
        large file's chunks is not something to do on the event loop; the whole
        point of serving a document is that this navigation did no such work.
        """
        if not self.document_preview_enabled():
            return
        searcher = self._app._search.searcher
        if searcher is None:
            return
        # Only skip while warming for this file is STILL RUNNING. Comparing
        # _warm_parent alone latched: it keeps the last file's id after the task
        # ends, so a file could never be warmed again.
        task = self._warm_task
        if self._warm_parent == parent_id and task is not None and not task.done():
            return
        query_sig = self._app._search.query_signature()

        async def begin() -> None:
            try:
                chunks = await asyncio.to_thread(searcher.get_file_chunks, parent_id)
            except Exception as exc:
                self.diag_log(f"warm bootstrap failed: {exc!r}")
                return
            if len(doc.chunks) >= len(chunks):
                return
            self.start_warming(parent_id, query_sig, width, doc, chunks)

        self._warm_bootstrap = asyncio.create_task(begin())

    def invalidate_documents_on_resize(self) -> None:
        """Drop captures that no longer match the pane width.

        Captured strips cannot be re-wrapped — re-deriving them means rebuilding
        the markdown tree, which is the cost this substrate avoids — so a width
        change invalidates. Served documents are keyed by pane width, and a
        stale one on screen is taken off so the next navigation rebuilds and
        re-captures instead of painting strips cut for the old width.

        Height-only changes must NOT invalidate: a vertical resize leaves every
        capture valid, and throwing them away would turn a window drag into a
        rebuild of everything the user had read.

        Written to be idempotent and timing-independent rather than to fire once
        on the "right" tick. ``call_after_refresh`` can still read the PRE-layout
        width — measured, it does — so a one-shot "has the width changed since
        last time?" guard latches the old value and then never fires again.
        Dropping everything that does not match the width measured right now is
        correct whenever it runs, and harmless to run repeatedly.
        """
        try:
            width = self._app.query_one("#preview_pane", VerticalScroll).content_size.width
        except Exception:
            return
        if width <= 0:
            return
        dropped = self.document_store.drop_other_widths(width)
        dropped += self.capture_store.drop_other_widths(width)
        view = self.document_view
        stale_view = (
            view is not None
            and not view.has_class("-hidden")
            and self._document_view_width not in (0, width)
        )
        if stale_view:
            # Strips cut for the old width would paint clipped or short. Take it
            # off screen and rebuild through the widget path, which re-captures
            # at the new width on its way.
            self.hide_document_view()
            parent_id, target = self.parent_id, self.cursor_target()
            if parent_id is not None and target is not None and target[0] == parent_id:
                self.render_full_doc(parent_id, focus_chunk_seq=target[1])
        if dropped or stale_view:
            self.diag_log(f"documents invalidated width={width} dropped={dropped}")

    def hide_document_view(self) -> None:
        """Take the document preview off screen when another substrate wins."""
        with contextlib.suppress(Exception):
            self._app.query_one("#preview_pane", VerticalScroll).remove_class("-document")
        view = self.document_view
        if view is None:
            return
        # Textual does not re-home focus when the focused widget is hidden, so
        # hiding a focused document view left `app.focused` None — measured, a
        # subsequent key press never reached the results tree, which reads as
        # the whole UI going dead until the user clicks.
        had_focus = bool(getattr(view, "has_focus", False))
        with contextlib.suppress(Exception):
            view.add_class("-hidden")
        if had_focus:
            with contextlib.suppress(Exception):
                self._app.query_one("#results_pane").focus()

    def begin_reconcile_scroll(self) -> None:
        self.reconciling = True

    def end_reconcile_scroll(self) -> None:
        self.reconciling = False

    def swap_reveal_target(
        self, target: Widget, margin: int, anchor_region: Region | None = None
    ) -> bool:
        """Atomic preview swap: hide the outgoing container, position the
        incoming one so ``target`` sits ``margin`` rows down, and reveal it —
        all in one tick. Returns True when a swap happened, False when there is
        no outgoing container (the caller then scrolls + reveals normally).

        The outgoing container stayed on screen through the whole build, so the
        first frame the user sees after this is the new preview already at its
        match — no blank, no scroll-into-place. The anchor's offset is taken
        relative to the incoming container's top, which is scroll-independent
        and so survives the outgoing container leaving the layout.

        ``anchor_region`` (screen space) overrides ``target.region`` when given:
        a match inside a table renders as one full-height DataTable, so the
        matched cell is not its own widget — the caller resolves the cell's
        region and passes it here so the swap lands on the matched row, not the
        table top."""
        outgoing = self.outgoing
        new = self.active
        if outgoing is None or new is None or outgoing is new:
            return False
        anchor_y = (anchor_region if anchor_region is not None else target.region).y
        offset = anchor_y - new.region.y
        target_y = max(0, offset - margin)
        pane = self._app.query_one("#preview_pane", VerticalScroll)
        outgoing.add_class("-hidden")
        pane.scroll_to(y=target_y, animate=False, immediate=True)
        self.diag_log(f"scroll site=swap y={target_y}")
        new.remove_class("-pre-reveal")
        # Same event as reveal()'s, on the atomic-swap path — see the note there.
        self.diag_log(f"first_paint parent={new.parent_doc_id[:8]} path=swap")
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
        # A served document is an outgoing substrate too — held on screen
        # through the build so the pane never blanks, and dropped only now that
        # there is something to replace it with.
        self.hide_document_view()
        container.remove_class("-pre-reveal")
        # The moment the new result becomes visible. Logged because it is the
        # only exact answer to "when did the user first see this?" — inferring
        # it by diffing captured frames measures how MUCH of the pane changed,
        # not when it changed, and mis-ranks any design that paints early and
        # fills in behind it.
        self.diag_log(f"first_paint parent={container.parent_doc_id[:8]}")
        # Revealed — the bounded-time backstop is no longer needed.
        self._cancel_reveal_watchdog()
        # The new result is now positioned — re-measure the ▲/▼ view markers.
        # This is the authoritative switch event: it fires even when the reveal
        # scroll doesn't move (which the scroll-watcher trigger would miss,
        # leaving the previous result's markers stale).
        with contextlib.suppress(Exception):
            self._app._match_nav.on_result_revealed()

    def reveal_active(self) -> None:
        """Invariant backstop: the active container must not stay ``-pre-reveal``
        (invisible) once its navigation has settled. Reveal is normally driven by
        one specific finalize task, but rapid navigation can cut that task short
        before it reveals — and the scroll-only resume path
        (:meth:`_settled_instant_scroll`) never reveals at all. Either leaves the
        active container built-but-invisible: the "preview blank until I select a
        different result and come back" strand. Calling this whenever a
        navigation settles closes every such gap, and also finishes the cut-short
        finalize's bar/latch cleanup (below). A no-op only when nothing is active."""
        container = self.active
        if container is None:
            return
        if container.has_class("-pre-reveal"):
            self.reveal(container)
        # Whether we just revealed a cut-short mount or arrived here on a
        # scroll-only Branch-A settle, the navigation has landed: nothing is
        # loading for the active container. A cut-short or superseded mount can
        # still leave the SHARED progress bar open (a prior mount opened it and
        # the winning path never closed it) and the in-flight latch set. Finish
        # the finalize's terminal cleanup so the bar can't stick ("mount stuck at
        # 49%") and a re-select of the same result isn't deduped out.
        self.hide_progress_bar()
        self.inflight_target = None

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
        """Reveal-safe wrapper around :meth:`_finalize_via_lock_body`.

        The body reveals by scheduling ``reconcile`` at its very end; if rapid
        navigation CANCELS this task at any earlier await (build/settle wait), it
        never gets there and the still-active container would stay ``-pre-reveal``
        (invisible) — the strand. The ``finally`` backstop reveals the active
        container in that cut-short case so it can't stay hidden. On the normal
        path the body returns having scheduled the reveal, so the backstop is a
        no-op (and never reveals early, preserving the no-flash-at-top scroll)."""
        reveal_scheduled = False
        try:
            reveal_scheduled = await self._finalize_via_lock_body(
                container, focus_chunk_seq, t0, expected_above_seqs=expected_above_seqs, path=path
            )
        finally:
            # Only repair the container THIS finalize owns. On a supersede
            # cancellation self.active is already the successor; revealing it
            # here would lift its -pre-reveal before its own scroll settled (a
            # content flash) and clear the successor's shared bar/latch. When a
            # successor has taken over, its own finalize/watchdog reveals it.
            if not reveal_scheduled and container is self.active:
                self.reveal_active()

    async def _finalize_via_lock_body(
        self,
        container: PreviewContainer,
        focus_chunk_seq: int,
        t0: float,
        *,
        expected_above_seqs: list[int] | None = None,
        path: str = "cold_via_lock",
    ) -> bool:
        """Wait for *every* chunk above the focus in the mounted window
        to finish building before revealing + scrolling. Awaiting only
        the focus chunk's ``build_done`` (the previous behaviour) let
        the scroll land while siblings above were still height=0; once
        those grew, the focus chunk's virtual_y shifted and the user
        saw the correct match flash, then jump to an unrelated area.
        Waiting for the above-siblings means the focus chunk's
        virtual_y is final at scroll time. Returns True once the reveal has
        been scheduled (so the wrapper's backstop knows it need not fire)."""
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
        # ``display=False`` widgets take no part in the arrange, so they cannot
        # move the match and there is nothing to wait for. Skipping them is also
        # load-bearing for ``await_match_settled``, which requires every watched
        # height to be > 0 — a hidden widget measures 0 forever and would hold
        # the settle to its round limit.
        above_widgets: list[FNDMarkdown] = [
            w
            for seq, w in container.chunk_widgets.items()
            if seq < focus_chunk_seq and isinstance(w, FNDMarkdown) and w.display
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

        # A prefetch/eviction race can detach the active container mid-mount;
        # re-attach it (chunk tree intact) so the reveal lands on real geometry
        # instead of a zero-region ghost (blank pane).
        await self._reattach_active_if_detached(container)
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
        return True

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
            # Re-attach if a race detached the active container mid-mount (see
            # _reattach_active_if_detached) so the reveal isn't on a ghost.
            await self._reattach_active_if_detached(container)
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
        # Reveal-on-settle: this resume path is taken for an already-mounted
        # target, which is USUALLY already visible — but if a prior rapid-nav
        # cancel left it ``-pre-reveal`` (invisible), scrolling alone would strand
        # it. ``reveal_active`` lifts it once the scroll lands (no-op otherwise).
        self._app._preview_scroll.reconcile(self.reveal_active)
        # Re-freeze whatever this navigation left live outside the new window.
        # The sweep after the initial fill is not enough on its own: lazy mount
        # and each subsequent navigation mount more chunks live, so DOM creeps
        # back over a reading session — measured 402 widgets after the fill,
        # 805 after twenty navigations. Freezing has to recur for the same
        # reason the fill does.
        await self._refreeze_around(container, focus_chunk_seq)

    async def _refreeze_around(self, container: PreviewContainer, focus_chunk_seq: int) -> None:
        """Freeze everything outside the window centred on ``focus_chunk_seq``."""
        chunks = self.chunk_cache.get(container.parent_doc_id)
        if not chunks:
            return
        focus_idx = next((i for i, c in enumerate(chunks) if c.chunk_seq == focus_chunk_seq), None)
        if focus_idx is None:
            return
        # Only the focus chunk stays live. Keeping the whole visible window live
        # was the first cut and it buys nothing: both policies settle at the same
        # DOM (263 widgets on a 99-chunk file), because each navigation re-freezes
        # whatever the last one left behind. Freezing the window as well simply
        # gets there sooner — 401 widgets after the initial fill against 688.
        win_start = focus_idx
        win_end = focus_idx + 1
        await self._freeze_chunks_outside_window(container, chunks, win_start, win_end)
        # Reclaim here too, not only on the paths that rebuild. The sweep used to
        # run on cross-file dispatch and the same-file rebuild alone, so a
        # container stranded by an earlier navigation survived indefinitely while
        # the user stayed in one file: measured twelve in-window navigations
        # later, one strand still held 6 live chunks and 169 widgets — more DOM
        # than the 92 frozen chunks of the file actually being read.
        self.sweep_stranded_containers()

    def bump_reset_generation(self) -> None:
        """Invalidate any in-flight mount. Call from every path that clears the
        preview caches + DOM (new query, scope clear, highlight rerender): the
        cancelled mount's deferred `finally` sees the changed generation and
        drops its now-stale container instead of re-caching it. Navigation /
        resume paths that cancel a mount but keep the cache must NOT call this —
        their partial container is still wanted for a later resume."""
        self.reset_generation += 1
        # A new query / scope change is a fresh start: the next navigation gets
        # its own repair budget rather than inheriting a spent one.
        self._paint_repair_target = None
        # Stop warming immediately rather than at the next batch boundary. Its
        # captures carry the OLD query's highlighting, so every chunk it builds
        # from here is work whose result can never be served — the store key
        # carries the query signature, so it would be stored and never read.
        task = self._warm_task
        if task is not None and not task.done():
            task.cancel()
        self._warm_parent = None
        # Captures for a superseded query can never be served either; holding
        # them only spends the row budget that the new query's captures need.
        self.document_store.clear()
        self.capture_store.clear()
        # Coverage is captures too: everything it is building carries the old
        # query's highlighting and is filed under a key nothing will read.
        cover = self._coverage_task
        if cover is not None and not cover.done():
            cover.cancel()
        # And take the VIEW down with them. Clearing the store alone left the
        # previous query's document painted with its old highlighting — measured
        # to survive a no-match query, where the tree is empty and the paint
        # check returns early because there is no cursor target to compare.
        self.hide_document_view()

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
        self.decode_worker = None
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
        # This decode is done. (A cancelled worker never reaches either callback,
        # but ``is_finished`` covers CANCELLED too, so pipeline_busy self-clears.)
        self.decode_worker = None
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

    def above_window_start(self, chunks: list[FileChunk], focus_idx: int, viewport_h: int) -> int:
        """First chunk index to mount above ``focus_idx``.

        Counted in ROWS, not chunks. Everything mounted above the focus has to
        finish building before the match can be revealed, so this is the part of
        the mount the user waits on — and a fixed chunk count prices it wrongly
        for every format at once. A PDF chunk is a page (30-60 rows), so seven of
        them is several screens of content nobody asked for; a markdown chunk is
        one heading's section (often 2-3 rows), so seven of them is less than the
        context margin the scroll wants to leave above the match.

        So: walk up until roughly a screenful of content is covered, bounded by
        ``VISIBLE_FIRST_ABOVE`` chunks either way. Estimated from the source line
        count rather than measured geometry — nothing is laid out yet, and the
        estimate only has to be good enough to stop one chunk early or late.
        """
        rows_wanted = max(1, int(viewport_h * tuning.VISIBLE_FIRST_ABOVE_SCREENS))
        floor = max(0, focus_idx - tuning.VISIBLE_FIRST_ABOVE)
        rows = 0
        start = focus_idx
        for i in range(focus_idx - 1, floor - 1, -1):
            start = i
            rows += (chunks[i].body_md or chunks[i].body_text or "").count("\n") + 1
            if rows >= rows_wanted:
                break
        return start

    async def _freeze_chunks_outside_window(
        self, container: PreviewContainer, chunks: list[FileChunk], win_start: int, win_end: int
    ) -> None:
        """Swap every background-filled chunk for its frozen capture.

        Phase 3 exists so an intra-file jump lands on an already-mounted chunk
        rather than rebuilding. That works, and it is expensive: a fully-filled
        file measured 99 chunks holding 2,735 widgets, and Textual's arrange is
        linear in widget count, so the whole file's DOM taxes every interaction.
        Freezing keeps what Phase 3 buys and drops what it costs — the chunk is
        still there to jump to, at one widget instead of ~28.

        Runs as one pass after the fill rather than per chunk during it: a chunk
        mounted a moment ago has not been laid out, ``size.height`` is 0, and
        ``freeze`` rightly refuses it. Attempting it inline failed on all 72
        chunks of a real file.

        The visible window is left live — those chunks are being read, and the
        focused one is what the scroll resolves against.

        On by default; ``_FND_NO_FREEZE=1`` opts out. Measured on a real
        99-chunk file: 2,735 -> 263 widgets with every chunk still mounted and
        jumpable, and no behavioural difference in a real terminal beyond the
        DOM. The swap is layout-neutral: across 47 real chunks the capture's
        height matched the height the widget tree occupied every time.
        """
        import contextlib
        import os as _os

        from fnd.tui.preview.frozen import FrozenChunkView, freeze

        if _os.environ.get("_FND_NO_FREEZE") == "1":
            return
        await self.await_settled()
        if self.active is not container:
            return
        # Never capture from a container that is not being PAINTED. The incoming
        # container builds invisibly (`-pre-reveal`, opacity 0) and is revealed
        # only once its scroll lands, so a sweep that runs before the reveal
        # captures correctly-sized, completely BLANK strips — and nothing
        # downstream can tell, because the geometry is right. That is the
        # end-of-file blank: the tail is captured pre-reveal, then served empty.
        # Wait for the reveal; a later landing re-freezes via _refreeze_around.
        for _ in range(tuning.FREEZE_REVEAL_WAIT_TICKS):
            if not container.has_class("-pre-reveal") and not container.has_class("-hidden"):
                break
            await asyncio.sleep(0.05)
        else:
            self.diag_log("freeze skipped — container never revealed")
            return
        try:
            pane_width = self._app.query_one("#preview_pane", VerticalScroll).content_size.width
        except Exception:
            pane_width = 0
        frozen = 0
        for index, chunk in enumerate(chunks):
            # Only the VISIBLE window stays live. Skipping everything before
            # win_end — as the first cut did — left every chunk ABOVE the focus
            # live too, which is most of them on a file the user has read down
            # through: 11 live chunks holding 329 widgets against 72 frozen
            # holding 72.
            if win_start <= index < win_end or index not in container.mounted_indices:
                continue
            widget = container.chunk_widgets.get(chunk.chunk_seq)
            if not isinstance(widget, FNDMarkdown):
                continue
            captured = freeze(widget, chunk.chunk_seq)
            if captured is None:
                continue
            view = FrozenChunkView(captured)
            try:
                widget.parent.mount(view, before=widget)  # type: ignore[union-attr]
            except Exception:
                continue
            # Keep it. This capture used to die with the container, so a jump
            # outside the mounted window rebuilt chunks that had already been
            # built and captured moments earlier — which is why revisiting a
            # match cost exactly as much as visiting it the first time.
            #
            # Keyed on the PANE width, not the capture's own: a chunk renders
            # narrower than the pane by the container's padding (measured 63
            # against a 64-wide pane), so keying on the capture's width would
            # miss on every lookup.
            if pane_width > 0:
                self.capture_store.put(
                    container.parent_doc_id, container.query_signature, pane_width, captured
                )
            container.chunk_widgets[chunk.chunk_seq] = view
            container.match_targets.pop(chunk.chunk_seq, None)
            with contextlib.suppress(Exception):
                widget.remove()
            frozen += 1
        if frozen:
            self.diag_log(f"backfill froze={frozen} chunks")
        self._harvest_document(container, chunks)

    def _harvest_document(self, container: PreviewContainer, chunks: list[FileChunk]) -> None:
        """Keep this file's captures so the next visit is a scroll, not a rebuild.

        Runs straight after the freeze sweep, when most chunks are already
        captured and the rest are laid out — the only moment most of the file is
        capturable at once. The window chunks the sweep leaves live are captured
        WITHOUT being removed: the document is a shadow copy, not the thing on
        screen, and it must be contiguous or every row after a gap is wrong.
        """
        if not self.document_preview_enabled():
            return
        try:
            width = self._app.query_one("#preview_pane", VerticalScroll).content_size.width
        except Exception:
            return
        if width <= 0:
            return
        doc = self.document_store.capture(container, chunks)
        if doc is None:
            self.diag_log(f"harvest nothing-capturable parent={container.parent_doc_id[:8]}")
            return
        # Keyed on the PANE width, not the capture's own width: the pane is what
        # can be measured at serve time, and a chunk renders narrower than the
        # pane by the container's padding (measured 63 against a 64-wide pane),
        # so keying on doc.width would miss on every lookup.
        existing = self.document_store.get(
            container.parent_doc_id, container.query_signature, width
        )
        if existing is not None and len(existing.chunks) >= len(doc.chunks):
            return
        self.document_store.put(container.parent_doc_id, container.query_signature, width, doc)
        self.diag_log(
            f"harvest parent={container.parent_doc_id[:8]} chunks={len(doc.chunks)}/{len(chunks)} "
            f"rows={doc.total_rows} width={doc.width}"
        )
        if len(doc.chunks) < len(chunks):
            self.start_warming(
                container.parent_doc_id, container.query_signature, width, doc, chunks, container
            )

    def hit_indices(self, parent_id: str, chunks: list[FileChunk]) -> list[int]:
        """Indices of the chunks the results list can navigate to in this file.

        The listed hits, not every textual match: they are what Down/Up step
        through, and they are already capped per file by
        ``defaults.sections_per_file_max``, which bounds coverage for free.
        """
        groups = getattr(self._app._search, "groups", None) or []
        seqs: set[int] = set()
        for group in groups:
            if group.parent_id == parent_id:
                seqs = {h.chunk_seq for h in (getattr(group, "hits", None) or [])}
                break
        if not seqs:
            return []
        return [i for i, c in enumerate(chunks) if c.chunk_seq in seqs]

    def start_coverage(self, parent_id: str, focus_chunk_seq: int) -> None:
        """Re-plan coverage around the cursor, as its OWN task.

        Cursor-driven rather than mount-driven: it runs for every navigation,
        including the ones served instantly from an already-mounted chunk, which
        is exactly when there is spare time to capture ahead.

        Never awaited by the mount. Coverage captures for as long as the plan has
        targets, and ``lazy_mount.check`` bails while ``user_mount_in_flight()``
        — so awaiting it from ``_mount_chunks_async`` stopped upward scrolling
        working for its whole run. The mount is over when the preview is on
        screen; capturing ahead is separate work.

        The newest cursor wins: the plan is ordered by distance from where the
        user IS, so an in-flight run for the previous position is working on the
        wrong end of the list.
        """
        import os as _os

        if _os.environ.get("_FND_NO_COVERAGE") == "1":
            return
        if self._coverage_anchor == (parent_id, focus_chunk_seq):
            task = self._coverage_task
            if task is not None and not task.done():
                # Same cursor, already running. Restarting here is how coverage
                # stalls: repeated dispatches for one position would cancel the
                # run mid-capture forever and never get past the first chunk.
                return
        self._coverage_anchor = (parent_id, focus_chunk_seq)
        task = self._coverage_task
        if task is not None and not task.done():
            task.cancel()
        self._coverage_task = asyncio.create_task(self._run_coverage(parent_id, focus_chunk_seq))

    async def _run_coverage(self, parent_id: str, focus_chunk_seq: int) -> None:
        """Capture what navigation will reach for next, in priority order.

        Captures land in ``capture_store``, never in the pane: the mounted set
        has to stay CONTIGUOUS — lazy mount only fills at its edges, so a hole
        in the middle would be a stretch of the document silently missing —
        while what navigation needs covered is the matches, scattered right
        through the file and across its neighbours. A capture costs 44.5 KB and
        no arrange time, which is what lets the cache hold a set the DOM cannot.

        Building happens on the warm host, off the visible screen, so a file's
        worth of markdown builds without inflating the arrange of the widget the
        user is reading.
        """
        if tuning.COVERAGE_CHUNK_BUDGET <= 0:
            return
        # Settle first. Coverage is armed at the START of a navigation and
        # re-planned on every one, so a held-down arrow key would otherwise
        # spawn a plan per keypress — each decoding neighbouring files off the
        # loop before being cancelled by the next. Sleeping first means a
        # superseded plan does NO work at all: the cancel lands here.
        await asyncio.sleep(tuning.PREVIEW_WARM_DELAY)
        for _ in range(tuning.PREVIEW_WARM_YIELD_TICKS):
            if not self._app._preview_scroll.is_settling:
                break
            await asyncio.sleep(0.05)
        try:
            pane = self._app.query_one("#preview_pane", VerticalScroll)
        except Exception:
            return
        # WAIT for a measurable width rather than giving up on one. Coverage is
        # armed at the START of a navigation, which on a cold start is before
        # the pane has been laid out — and since nothing re-triggers it until
        # the cursor moves again, returning here meant the first file of a
        # session was never covered at all.
        width = 0
        for _ in range(tuning.PREVIEW_WARM_YIELD_TICKS):
            width = pane.content_size.width
            if width > 0:
                break
            await asyncio.sleep(0.05)
        if width <= 0:
            return
        query_sig = self._app._search.query_signature()
        # A SNAPSHOT: covering runs for seconds, and reading the live spec
        # mid-run files chunks highlighted for a new query under the old key,
        # where nothing will ever read them.
        spec = self._app._effective_match_spec
        groups = getattr(self._app._search, "groups", None) or []
        ids = [g.parent_id for g in groups]
        here = ids.index(parent_id) if parent_id in ids else -1

        captured = 0
        try:
            # Tier 1 and 2: the hits of the current file, then of the
            # neighbours, outward from the cursor. Tier 2 is what makes moving
            # BETWEEN files served rather than built.
            plan = [parent_id]
            if here >= 0:
                plan += neighbour_order(ids, here, tuning.COVERAGE_NEIGHBOUR_FILES)
            for pid in plan:
                # Decide whether this file needs anything BEFORE decoding it.
                # A neighbour has usually never been opened, so covering it
                # means a full chunk decode — and re-deciding that on every
                # cursor move meant decoding the same four neighbours over and
                # over, off the loop, competing with the landing. The listed
                # hits carry their chunk_seq, so "already covered" is answerable
                # from the store alone.
                if not self._file_needs_coverage(pid, query_sig, width):
                    continue
                chunks = await self._coverage_chunks(pid)
                if not chunks:
                    continue
                focus_idx = (
                    next((i for i, c in enumerate(chunks) if c.chunk_seq == focus_chunk_seq), 0)
                    if pid == parent_id
                    else self._landing_index(pid, chunks)
                )
                targets = coverage_targets(
                    total=len(chunks),
                    focus_idx=focus_idx,
                    hit_indices=self.hit_indices(pid, chunks),
                    already=self._held_indices(pid, query_sig, width, chunks),
                    margin=tuning.COVERAGE_MARGIN,
                    budget=tuning.COVERAGE_CHUNK_BUDGET,
                )
                captured += await self._capture_targets(
                    pid, query_sig, width, chunks, targets, spec
                )

            # Tier 3: the rest of the CURRENT file, and only now. Covering a
            # file whole spends ~30s of the one serial host on chunks no jump
            # lands on, while the neighbours — the buffer the cursor actually
            # needs — get nothing. Its only prize is that a scroll into the gaps
            # between matches finds them ready, and lazy mount already handles
            # that fast enough. So it earns its place strictly as idle work.
            chunks = await self._coverage_chunks(parent_id)
            if chunks:
                focus_idx = next(
                    (i for i, c in enumerate(chunks) if c.chunk_seq == focus_chunk_seq), 0
                )
                filler = filler_targets(
                    total=len(chunks),
                    focus_idx=focus_idx,
                    already=self._held_indices(parent_id, query_sig, width, chunks),
                    budget=tuning.COVERAGE_CHUNK_BUDGET,
                )
                captured += await self._capture_targets(
                    parent_id, query_sig, width, chunks, filler, spec
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - best effort
            self.diag_log(f"coverage failed: {exc!r}")
        if captured:
            self.diag_log(
                f"coverage parent={parent_id[:8]} captured={captured} "
                f"rows={self.capture_store.total_rows()}"
            )

    def _file_needs_coverage(self, parent_id: str, query_sig: str, width: int) -> bool:
        """Whether any listed hit of this file is still uncaptured.

        Answered from the results list and the store, with no decode: a hit
        carries its chunk_seq, which is exactly the store's key. That is what
        makes re-planning on every cursor move cheap — without it, each move
        re-decoded every neighbour just to discover there was nothing to do.
        """
        groups = getattr(self._app._search, "groups", None) or []
        for group in groups:
            if group.parent_id != parent_id:
                continue
            hits = getattr(group, "hits", None) or []
            return any(
                self.capture_store.get(parent_id, query_sig, width, h.chunk_seq) is None
                for h in hits
            )
        return False

    def _landing_index(self, parent_id: str, chunks: list[FileChunk]) -> int:
        """Where a jump INTO this file would land — its first listed hit."""
        hits = self.hit_indices(parent_id, chunks)
        return hits[0] if hits else 0

    def _held_indices(
        self, parent_id: str, query_sig: str, width: int, chunks: list[FileChunk]
    ) -> set[int]:
        store = self.capture_store
        return {
            i
            for i, c in enumerate(chunks)
            if store.get(parent_id, query_sig, width, c.chunk_seq) is not None
        }

    async def _coverage_chunks(self, parent_id: str) -> list[FileChunk]:
        """This file's chunks, decoded off the event loop if not already known.

        A neighbour has usually never been opened, so its chunks are not in the
        cache. Decoding a large file's chunks is not something to do on the
        event loop — that is what froze the indexer portal.
        """
        cached = self.chunk_cache.get(parent_id)
        if cached:
            return cached
        searcher = self._app._search.searcher
        if searcher is None:
            return []
        try:
            return await asyncio.to_thread(searcher.get_file_chunks, parent_id)
        except Exception:
            return []

    async def _capture_targets(
        self,
        parent_id: str,
        query_sig: str,
        width: int,
        chunks: list[FileChunk],
        targets: list[int],
        spec: MatchSpec,
    ) -> int:
        """Capture ``targets`` of one file, standing down for anything on screen."""
        captured = 0
        for index in targets:
            if self._app._search.query_signature() != query_sig:
                return captured
            try:
                pane = self._app.query_one("#preview_pane", VerticalScroll)
            except Exception:
                return captured
            if pane.content_size.width != width:
                # A reflow (Reading View, a resize) invalidates every capture
                # cut at the old width — the key carries it, so they can never
                # be served. Carrying on would spend the one serial host on work
                # that is discarded, while competing with the reflow itself.
                self.diag_log("coverage stopped — pane width changed mid-run")
                return captured
            chunk = chunks[index]
            if not uses_markdown_renderer(chunk):
                # Plain-layout chunks have no off-screen builder; they stay on
                # the live mount path and nothing here can serve them.
                continue
            # Never compete with a landing the user is waiting on.
            for _ in range(tuning.PREVIEW_WARM_YIELD_TICKS):
                if not self._app._preview_scroll.is_settling:
                    break
                await asyncio.sleep(0.05)
            capture = await self._warm_host.capture(chunk, width, match_spec=spec)
            if capture is not None:
                self.capture_store.put(parent_id, query_sig, width, capture)
                captured += 1
            await asyncio.sleep(0)
        return captured

    def start_warming(
        self,
        parent_id: str,
        query_sig: str,
        width: int,
        doc: FrozenDocument,
        chunks: list[FileChunk],
        container: PreviewContainer | None = None,
    ) -> None:
        """Grow ``doc`` towards covering its file, in the background.

        Called from BOTH paths. Harvest reaches it after a widget-path mount,
        but the document path returns before harvest, so a file served from the
        store could only warm if it had once been built the slow way — meaning
        warming helped the first file of a session and no other.
        """
        task = self._warm_task
        if task is not None and not task.done():
            if self._warm_parent == parent_id:
                # Already growing THIS file. Restarting on every navigation is
                # how coverage stalled: each one cancelled the task mid-batch
                # and started again from the same short run.
                return
            task.cancel()
        self._warm_parent = parent_id
        self._warm_task = asyncio.create_task(
            self._warm_document(parent_id, query_sig, chunks, width, doc, container)
        )

    async def _warm_document(
        self,
        parent_id: str,
        query_sig: str,
        chunks: list[FileChunk],
        width: int,
        doc: FrozenDocument,
        container: PreviewContainer | None = None,
    ) -> None:
        """Extend a captured run to cover the rest of the file.

        A run typically stops short because the background fill bails the moment
        the user takes scroll control, so the tail is missing and a jump there
        falls back to a rebuild — the slow path the substrate exists to avoid.
        This mounts the missing chunks into the container that is already on
        screen, captures them, and extends the document at whichever end they
        belong to.

        Growth stays CONTIGUOUS by construction: only chunks immediately before
        or after the run are taken, and the first failure at an end stops that
        direction. A gap would shift every row after it.

        Three rules keep this from becoming the very thing the pipeline work
        removed — background filling that competes with the landing the user is
        waiting on. Measured on the real corpus, an earlier version that broke
        all three made WTE navigation 170ms -> 579ms median:

        1. It yields to the scroll. Nothing is mounted while the controller is
           settling; that is the same signal ``lazy_mount`` already respects.
        2. It mounts in BATCHES with one settle per batch, not a settle per
           chunk. Per-chunk settling was slow enough that a 117-chunk file
           reached 33 before the next navigation cancelled it.
        3. It only grows UPWARD when the container is off screen. Mounting above
           the viewport in a VISIBLE container shoves the content down — the
           reason the original background fill deliberately filled below only.
           When the document view is on screen the container is hidden, and the
           view's own prepend moves extent and offset together, so upward growth
           is invisible there.
        """
        from fnd.tui.preview.frozen import freeze

        # Snapshot the highlighting this document is FOR. Warming runs for
        # seconds; reading the app's live spec per chunk meant a query change
        # mid-batch produced chunks highlighted for the new query, appended to a
        # document filed under the old query's key where nothing can correct it.
        warm_spec = self._app._effective_match_spec

        def still_valid() -> bool:
            """Warming survives everything except the query it is FOR changing.

            Requiring ``is_live(container)`` tied coverage to a widget tree that
            navigation replaces, so warming died on the first jump — that capped
            coverage at 83 of 1018 chunks. Comparing ``reset_generation`` then
            killed it for scope changes and highlight re-renders too, which do
            not invalidate a capture; measured, warming then stopped before
            capturing a single chunk. The query signature is what actually
            decides whether these captures can ever be served, because it is
            what the store keys on.
            """
            try:
                return self._app._search.query_signature() == query_sig
            except Exception:
                return False

        def live_view() -> FrozenDocumentView | None:
            view = self.document_view
            return view if view is not None and view.document is doc else None

        async def yield_to_navigation() -> bool:
            """Wait out an in-flight landing. False if warming should stop."""
            # Stop before this one document exceeds what the whole cache may
            # hold. The budget scales with system memory, so on a large machine
            # this effectively never fires and a huge file warms whole; on a
            # small one it degrades instead of exhausting the machine.
            if doc.total_rows >= frozen_store.budget_rows():
                self.diag_log(f"warm stopped at row budget rows={doc.total_rows}")
                return False
            for _ in range(tuning.PREVIEW_WARM_YIELD_TICKS):
                if not still_valid():
                    return False
                if not self._app._preview_scroll.is_settling:
                    return True
                await asyncio.sleep(0.05)
            return still_valid()

        async def capture_batch(indices: list[int]) -> list[FrozenChunk]:
            """Capture a batch OFF-SCREEN, one chunk at a time.

            Nothing is mounted into the on-screen container: building there is
            what made warming compete with the landing, and it inflated the very
            DOM freezing exists to shrink. An already-captured chunk is reused
            rather than rebuilt.
            """
            out: list[FrozenChunk] = []
            alive = container is not None and is_live(container)
            for index in indices:
                chunk = chunks[index]
                # The container is a SHORTCUT for chunks already captured, not a
                # requirement — it may have been swept out from under us.
                widget = (
                    container.chunk_widgets.get(chunk.chunk_seq)
                    if alive and container is not None
                    else None
                )
                captured: FrozenChunk | None = None
                if isinstance(widget, FrozenChunkView):
                    captured = widget.frozen
                elif isinstance(widget, FNDMarkdown):
                    captured = freeze(widget, chunk.chunk_seq)
                else:
                    captured = await self._warm_host.capture(
                        chunk, doc.width or width, match_spec=warm_spec
                    )
                if captured is None:
                    break  # stop at the first gap; never skip past one
                out.append(captured)
            return out

        grew = 0

        def publish() -> None:
            """Make progress usable NOW, not at the end.

            Warming is routinely cancelled mid-flight by a file switch, and
            publishing only on completion threw away everything captured up to
            that point — the next visit then re-warmed from the same short run.
            A put is a dict assignment; the document is the same object either
            way. Defined outside the try so the cancellation handler can call it
            even when the cancel lands on the opening sleep.

            Skipped once the pane no longer matches the width these strips were
            cut for. ``put`` evicts captures of other widths, so a warm task
            that outlived a resize would wipe every correctly-sized document —
            and publishing per batch turns that from one bad write into a
            repeating one.
            """
            if not grew:
                return
            try:
                current = self._app.query_one("#preview_pane", VerticalScroll).content_size.width
            except Exception:
                return
            if current != width:
                return
            self.document_store.put(parent_id, query_sig, width, doc)

        try:
            await asyncio.sleep(tuning.PREVIEW_WARM_DELAY)
            seqs = [c.chunk_seq for c in chunks]
            first = seqs.index(doc.chunks[0].chunk_seq)
            last = seqs.index(doc.chunks[-1].chunk_seq)
            batch = tuning.PREVIEW_WARM_BATCH

            # Downward first: appending never moves the viewport, so it is safe
            # whichever substrate is on screen.
            cursor = last + 1
            while cursor < len(chunks):
                if not await yield_to_navigation():
                    break
                indices = list(range(cursor, min(cursor + batch, len(chunks))))
                captured = await capture_batch(indices)
                if not captured:
                    break
                for chunk_capture in captured:
                    view = live_view()
                    view.append(chunk_capture) if view else doc.append(chunk_capture)
                    grew += 1
                cursor += len(captured)
                publish()
                if len(captured) < len(indices):
                    break
                await asyncio.sleep(0)

            # Upward needs no gate now. The old one — "only when the container
            # is hidden" — was inverted against capability: a hidden container
            # is display:none, which is exactly when a capture returns None, so
            # the loop broke immediately and coverage stalled at 11 of 1018.
            # Building off-screen means nothing on screen can shift either way.
            cursor = first - 1
            while cursor >= 0:
                if not await yield_to_navigation():
                    break
                indices = list(range(max(0, cursor - batch + 1), cursor + 1))
                captured = await capture_batch(indices)
                if len(captured) < len(indices):
                    break
                for chunk_capture in reversed(captured):
                    view = live_view()
                    view.prepend(chunk_capture) if view else doc.prepend(chunk_capture)
                    grew += 1
                cursor -= len(captured)
                publish()
                await asyncio.sleep(0)

            if grew:
                self.diag_log(f"warm grew={grew} chunks={len(doc.chunks)}/{len(chunks)}")
        except asyncio.CancelledError:
            publish()
            raise
        except Exception as exc:  # pragma: no cover - warming is best-effort
            self.diag_log(f"warm failed: {exc!r}")

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

        # A container whose Prune is queued is dead: ``on_prune`` closes its
        # message loop, so re-mounting it does not bring it back. Dispatch
        # resolves reuse candidates through ``is_live`` precisely so we never
        # get here with one — this is the backstop that keeps a future caller
        # from silently building a preview into a widget that is on its way out.
        if is_condemned(container):
            self.diag_log(
                f"mount abandoned: container condemned parent={container.parent_doc_id[:8]}"
            )
            if self.active is container:
                self.active = None
            self.hide_progress_bar()
            self.inflight_target = None
            return

        needs_pre_reveal = not is_live(container) or container.has_class("-hidden")
        # Newly-mounted "above-window" widgets get hidden until phase 2b
        # finishes; the finally block makes sure every entry in this
        # list ends up displayed even on cancellation.
        hidden_widgets: list[Widget] = []

        # The try MUST cover the early awaits below (container mount,
        # cancel_task_on): they run BEFORE the detached finalize task is
        # spawned, and a cancellation here would otherwise skip the finally
        # entirely — stranding the progress bar with no task left to hide it.
        try:
            if not is_live(container):
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
                -1,
            )
            if focus_idx < 0:
                # The requested chunk isn't in the decoded list (a file past the
                # 5000-chunk decode ceiling, or hit/decode skew). Mounting around
                # chunk 0 and saying nothing let the user believe they were
                # looking at their result; the scroll then never finds a header
                # for the requested seq either. Say so, then fall back.
                self.diag_log(
                    f"mount seq={focus_chunk_seq} miss=not-in-decoded-chunks "
                    f"chunks={len(chunks)} — falling back to the first chunk"
                )
                focus_idx = 0
            win_start = self.above_window_start(chunks, focus_idx, pane.size.height or 40)
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

            # Phase 1b: mount the visible window, ABOVE the focus first and
            # closest-to-focus first within each side.
            #
            # The finalize waits only on the chunks ABOVE the focus — they are
            # what decides where the match lands — so mounting those first lets
            # the reveal stop waiting sooner. The ones below can arrive whenever;
            # interleaving the two sides made the reveal wait on roughly twice
            # the work it needed.
            #
            # Mounted in ONE pass. Awaiting each chunk's build before mounting
            # the next looks attractive — Textual pumps a Markdown's blocks
            # through the message loop, so builds started together interleave
            # and each reports ~755ms where one alone takes ~22-30ms — but it is
            # a trap under real use. Serialising means a navigation's mount must
            # finish before the next one can get going, and over a sustained
            # Down sweep on a 1018-chunk PDF that took the finalize's build wait
            # from a 1310ms median to 2832ms (worst case 10.6s), roughly doubling
            # end-to-end navigation. Measured over 40 presses; an 18-press sample
            # showed the opposite, which is why the bigger sample is the one to
            # trust here.
            above_first = range(focus_idx - 1, win_start - 1, -1)
            below_after = range(focus_idx + 1, win_end)
            for i in (*above_first, *below_after):
                if i in container.mounted_indices:
                    continue
                self.mount_chunk_into(container, chunks[i], i, chunks)
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
            # Wait for finalize to actually reveal (first paint) before adding
            # any DOM — otherwise this fill runs on the same coroutine and
            # starves the finalize task, delaying first paint several-fold.
            _ft = getattr(container, "_finalize_task", None)
            if _ft is not None:
                with contextlib.suppress(Exception):
                    await _ft
            await asyncio.sleep(0.05)
            if len(chunks) <= tuning.FULLMOUNT_CHUNK_BUDGET:
                batch_size = 6
                # Fill BELOW only: appending in document order grows content
                # DOWNWARD, so the match the user is reading never moves — no
                # flicker. Downward match-jumps land on a mounted chunk (instant).
                # We deliberately DON'T pre-fill ABOVE: inserting content above the
                # viewport shoves it down, and the scroll can only re-pin a frame
                # later (layout is async), so a passive above-fill always jitters
                # the viewport. Upward jumps instead rebuild on demand, served
                # from captures where coverage has them.
                #
                # This also decides how much of the file the in-file match count
                # can see: ``_count_stops`` walks the MOUNTED subtree, so a file
                # that stops being filled stops being counted in full. Coverage
                # below captures rather than mounts, so it deliberately does not
                # replace this.
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
            # Freeze AFTER the fill, not during it. A just-mounted chunk has
            # not been laid out — size.height is 0 — and a capture of an
            # unlaid-out widget is correctly refused, which is what made a
            # per-chunk attempt here fail every single time (72 of 72).
            await self._freeze_chunks_outside_window(container, chunks, win_start, win_end)

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
            elif not is_live(container):
                # A newer, same-query navigation swept this container out of the
                # DOM while this (now-cancelled) mount was in flight. Same-query
                # nav doesn't bump reset_generation, so the ``superseded`` branch
                # above doesn't catch it. Caching a dead container hands the next
                # visit a cache-HIT on a widget that isn't in the tree — the warm
                # path then activates + scrolls a zero-region ghost and reveals a
                # blank pane ("blank until I select another result and come
                # back"). ``is_live`` also rejects a container whose removal is
                # only QUEUED: it still reports a parent here, so the old
                # ``parent is None`` test cached it and reopened the same strand
                # one tick later. Never cache it, and drop any dangling reference.
                self.diag_log(
                    f"mount finally: container not live "
                    f"parent={container.parent_doc_id[:8]} — not caching"
                )
                if self.active is container:
                    self.active = None
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
                # background. ``protect=self.active`` is load-bearing, not
                # defensive: a stale mount's late finally must never evict the
                # container a newer nav re-activated (see PreviewCache.put).
                evicted = self.preview_cache.put(container, protect=self.active)
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

        # A capture, if coverage has one. This is where covering pays: mounting a
        # capture is one widget and no build, against parsing markdown and
        # waiting on Textual to pump a block tree through the message loop —
        # measured at 400-1274ms for the focus chunk alone. Every mount path
        # comes through here, so a cold mount, a rebuild and a lazy-mount batch
        # all serve captures without knowing about them.
        served = self._serve_capture(container, chunk, before=before_widget)
        if not served:
            # Structural renderer (markdown widget) for formats whose
            # extractor populated body_md; per-line plain layout for
            # everything else (PDF, TXT). Save current widgets-by-chunk_seq
            # so the mount helpers fill the per-container dicts.
            if uses_markdown_renderer(chunk):
                self._mount_structured_chunk(container, chunk, before=before_widget)
            else:
                self._mount_plain_chunk(container, chunk, before=before_widget)
        container.mounted_indices.add(index)

    def _serve_capture(
        self, container: PreviewContainer, chunk: FileChunk, *, before: Widget | None
    ) -> bool:
        """Mount ``chunk`` from its capture, or report that there isn't one.

        The width is read from the pane every time rather than cached: a capture
        is width-locked, and serving one cut at a stale width would paint the
        previous layout's line breaks into the current one.
        """
        try:
            width = self._app.query_one("#preview_pane", VerticalScroll).content_size.width
        except Exception:
            return False
        captured = self.capture_store.get(
            container.parent_doc_id, container.query_signature, width, chunk.chunk_seq
        )
        if captured is None:
            return False
        view = FrozenChunkView(captured)
        try:
            container.mount(view, before=before)
        except Exception:
            return False
        container.chunk_widgets[chunk.chunk_seq] = view
        # A frozen chunk has no inner match block to scroll to; the strategy
        # reads ``fnd_first_match_row`` off the view instead, exactly as it does
        # for chunks the freeze sweep replaced.
        container.match_targets.pop(chunk.chunk_seq, None)
        return True

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
        # Rebuild intra-file match-nav stops from the now-mounted chunks
        # (deferred a refresh so cell/block regions are laid out first). Runs
        # regardless of the scrollbar-marker toggle below.
        self._app.call_after_refresh(self._app._match_nav.rebuild)
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
            # Store the PLAIN string (not the rich Text) — the match-count /
            # stop-region scans regex over ``fnd_text`` and re needs a str.
            line_w.fnd_text = line_text.plain  # type: ignore[attr-defined]
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
        # Re-trigger the preview render for the focused result, through the same
        # cursor→target mapping the paint check reads (see cursor_target).
        target = self.cursor_target()
        if target is None:
            return
        parent_id, focus_chunk_seq = target
        self.render_full_doc(parent_id, focus_chunk_seq=focus_chunk_seq)

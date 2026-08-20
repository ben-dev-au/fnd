"""Scroll-driven lazy mounting for the structural preview.

``LazyMounter`` watches viewport position (via a debounced check) and
mounts the next batch of chunks when the user approaches the boundary
of the mounted region — long files behave like a continuous document
without the initial mount paying for everything.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.containers import VerticalScroll
from textual.widget import Widget

from fnd.tui.preview import tuning
from fnd.tui.preview.liveness import is_live
from fnd.tui.widgets.markdown import FNDMarkdown

if TYPE_CHECKING:
    from fnd.query import FileChunk
    from fnd.tui.app import FNDApp
    from fnd.tui.widgets.preview_container import PreviewContainer

__all__ = ["LazyMounter"]


class LazyMounter:
    """Owns the lazy-mount task and its debounce timer; one instance
    lives on the app for the session."""

    def __init__(self, app: FNDApp) -> None:
        self._app = app
        # In-flight lazy-mount task (driven by scroll). One at a time;
        # cleared on file switch alongside ``_preview_mount_task``.
        self.task: object | None = None
        # Monotonic-time gate. Programmatic scrolls (navigation anchor,
        # finalise reveal) push this forward so the watcher doesn't
        # interpret their own scroll changes as user intent and fire a
        # competing mount that yanks the focused chunk off-screen.
        # Debounce timer so rapid scroll bursts collapse to a single
        # check at the tail end — protects programmatic intermediate
        # scrolls AND smooths user wheel/key scroll bursts.
        self.check_timer: object | None = None

    def cancel(self) -> None:
        """Drop any in-flight scroll-driven mount task. Called on file
        switch / query change so the task can't mount stale chunks into
        a container the user has moved away from."""
        import contextlib

        if self.check_timer is not None:
            with contextlib.suppress(Exception):
                self.check_timer.stop()  # type: ignore[attr-defined]
            self.check_timer = None
        task = self.task
        if task is None:
            return
        try:
            done = task.done()  # type: ignore[attr-defined]
        except Exception:
            done = True
        if not done:
            with contextlib.suppress(Exception):
                task.cancel()  # type: ignore[attr-defined]
        self.task = None

    def schedule_check(self, *, user_initiated: bool = False) -> None:
        """Debounced entry point. Every scroll change re-arms a short
        timer; only the *last* scroll in a burst actually runs the
        check. Coalesces programmatic anchor scrolls (which fire one or
        two watcher trips back-to-back) AND user wheel/key bursts down
        to a single check at the tail end — no fighting between the
        navigation's own scroll-to-widget and lazy-mount's compensate."""
        import contextlib

        # A genuine user scroll (pane focused, and not one of the controller's
        # own reconcile scrolls) hands scroll control back to the user: release
        # the anchor so lazy-mount-on-scroll resumes. Programmatic scrolls from
        # navigation / container swaps trip this watcher too, but with the
        # results tree focused, so they don't release.
        if (
            user_initiated
            and self._app._preview_scroll.is_armed
            and not self._app._preview.reconciling
        ):
            self._app._preview_scroll.release()
        # A genuine user scroll also drops the match-nav burst memory, so the
        # next n/b is computed from the on-screen position rather than resuming
        # from the previous jump. Excludes the nav's own (reconcile-guarded)
        # scroll, which commits immediately inside the reconcile window.
        if user_initiated and not self._app._preview.reconciling:
            self._app._match_nav.on_manual_scroll()
        # Any scroll — a user wheel/key, a reveal, or a warm-nav result switch —
        # can move matches across the fold, so re-measure the ▲/▼ view markers.
        # Settle-gated inside, so it never reads regions mid cold-nav settle.
        self._app._match_nav.on_preview_scrolled()
        if self.check_timer is not None:
            with contextlib.suppress(Exception):
                self.check_timer.stop()  # type: ignore[attr-defined]
        self.check_timer = self._app.set_timer(0.12, self.check, name="lazy-mount-debounce")

    def check(self) -> None:
        """Scroll watcher entry point (after debounce). Mounts the next
        batch of chunks in the scroll direction when the viewport
        approaches a boundary of the chunk currently under it. Looks
        at the next *unmounted* chunk in document order — so gaps left
        behind when the user jumps between matches get filled
        progressively, not just the chunks past the absolute max/min
        mounted index."""
        self.check_timer = None
        # Suppress lazy-mount only while a navigation is still settling (the
        # controller owns the position until its scroll commits). Once the
        # reveal lands the gate opens, so user scrolls by ANY means — keyboard
        # OR an unfocused mouse-wheel — extend the window. Gating on is_armed
        # instead dead-ended wheel-scroll: the anchor stays armed across navs
        # and only release() (a focused user scroll) cleared it.
        if self._app._preview_scroll.is_settling:
            return
        container = self._app._preview.active
        if container is None:
            return
        chunks = self._app._preview.chunk_cache.get(container.parent_doc_id)
        if not chunks or not container.mounted_indices:
            return
        if len(container.mounted_indices) >= len(chunks):
            return
        # Don't compete with the initial visible-first mount task; it
        # owns the window and will hand off once it settles. Only until it has
        # PAINTED, though: the same task then goes on to fill and freeze in the
        # background, and waiting for that walled upward scrolling off for
        # seconds — mounting nothing at all until the housekeeping was done.
        if self._app._preview.mount_before_first_paint():
            return
        task = self.task
        if task is not None:
            try:
                if not task.done():  # type: ignore[attr-defined]
                    return
            except Exception:
                pass
        try:
            pane = self._app.query_one("#preview_pane", VerticalScroll)
        except Exception:
            return
        if pane.size.height <= 0:
            return

        scroll_y = float(pane.scroll_y)
        viewport_h = float(pane.size.height)
        viewport_bottom = scroll_y + viewport_h
        margin = float(tuning.LAZY_MOUNT_TRIGGER_MARGIN)

        # Snapshot mounted chunks' virtual-y ranges so we can find the
        # widgets covering viewport top + bottom in O(mounted) — small
        # under realistic mount counts.
        chunk_ranges: list[tuple[int, int, int]] = []
        for idx in sorted(container.mounted_indices):
            seq = chunks[idx].chunk_seq
            widget = container.chunk_widgets.get(seq)
            if widget is None:
                continue
            try:
                vr = widget.virtual_region  # type: ignore[attr-defined]
                y0 = int(vr.y)
                h = int(vr.height)
            except Exception:
                continue
            chunk_ranges.append((idx, y0, y0 + h))
        if not chunk_ranges:
            return

        def _covering(y: float) -> tuple[int, int, int] | None:
            for entry in chunk_ranges:
                if entry[1] <= y < entry[2]:
                    return entry
            return None

        import asyncio

        bottom_cover = _covering(viewport_bottom - 1) or chunk_ranges[-1]
        bottom_idx, _, bottom_chunk_y1 = bottom_cover
        # Only fire below if the IMMEDIATE next chunk in document order
        # is unmounted — otherwise the user is mid-region and can
        # scroll on through the contiguous mounted span without a wall.
        next_idx = bottom_idx + 1
        if (
            next_idx < len(chunks)
            and next_idx not in container.mounted_indices
            and (bottom_chunk_y1 - viewport_bottom) <= margin
        ):
            self.task = asyncio.create_task(
                self._mount_batch(container, chunks, start_idx=next_idx, direction="below")
            )
            return

        top_cover = _covering(scroll_y) or chunk_ranges[0]
        top_idx, top_chunk_y0, _ = top_cover
        prev_idx = top_idx - 1
        if (
            prev_idx >= 0
            and prev_idx not in container.mounted_indices
            and (scroll_y - top_chunk_y0) <= margin
        ):
            self.task = asyncio.create_task(
                self._mount_batch(container, chunks, start_idx=prev_idx, direction="above")
            )

    async def _mount_batch(
        self,
        container: PreviewContainer,
        chunks: list[FileChunk],
        *,
        start_idx: int,
        direction: str,
    ) -> None:
        """Mount ``tuning.LAZY_MOUNT_BATCH`` chunks starting at ``start_idx``,
        moving in ``direction``.

        Below: append in document order; no scroll adjustment needed
        because content grows downward.

        Above: hidden-prepend, await build, reveal. No scroll
        compensate — anchor preservation via virtual_region delta
        proved unreliable post-reveal (returns 0 on consecutive
        above-batches even after refresh), so newly-prepended chunks
        appear at the top of the visible area, which IS the right UX
        when the user just scrolled up to the wall.

        ``start_idx`` is the first index to mount in each direction;
        the loop skips already-mounted indices in case the gap was
        partially filled by an earlier batch.
        """
        import asyncio
        import contextlib

        if direction == "below":
            end = min(start_idx + tuning.LAZY_MOUNT_BATCH, len(chunks))
            for i in range(start_idx, end):
                if self._app._preview.active is not container:
                    return
                if i in container.mounted_indices:
                    continue
                try:
                    self._app._preview.mount_chunk_into(container, chunks[i], i, chunks)
                except Exception:
                    continue
                seq = chunks[i].chunk_seq
                md_widget = container.chunk_widgets.get(seq)
                if isinstance(md_widget, FNDMarkdown):
                    with contextlib.suppress(Exception):
                        async with md_widget.lock:
                            pass
                await asyncio.sleep(0)
            return

        # Mount chunks [start_idx, start_idx-1, …, start_idx-batch+1] in reverse
        # so each new widget lands BEFORE the anchor in document order, build
        # them hidden, then reveal AND scroll-compensate so the user's view stays
        # put. The anchor is the first already-mounted chunk just below the
        # prepend region; revealing the chunks above it shifts it DOWN by their
        # combined height, so we scroll the pane by that delta — turning the old
        # "wall, jump, scroll-down-to-retrigger" into a continuous upward scroll.
        # Measuring the delta reliably needs a SETTLED layout: the earlier code
        # read the delta as 0 pre-settle and gave up on compensation, leaving the
        # wall. ``_await_preview_settled`` (Textual's message-drain) makes it
        # reliable. ``hidden`` MUST be revealed even on cancel, else display=False
        # widgets cache as blank rows ("section only shows the heading").
        end = max(start_idx - tuning.LAZY_MOUNT_BATCH, -1)
        hidden: list[Widget] = []
        anchor_seq = chunks[start_idx + 1].chunk_seq if start_idx + 1 < len(chunks) else None
        try:
            for i in range(start_idx, end, -1):
                if self._app._preview.active is not container:
                    return
                if i in container.mounted_indices:
                    continue
                before_children = set(container.children)
                try:
                    self._app._preview.mount_chunk_into(container, chunks[i], i, chunks)
                except Exception:
                    continue
                for w in container.children:
                    if w not in before_children:
                        w.display = False
                        hidden.append(w)
                await asyncio.sleep(0.002)

            for w in hidden:
                if isinstance(w, FNDMarkdown):
                    with contextlib.suppress(Exception):
                        async with w.lock:
                            pass

            # Capture the anchor's content position + pane scroll just before the
            # reveal grows the content above it.
            pane = self._app.query_one("#preview_pane", VerticalScroll)
            anchor_w = container.chunk_widgets.get(anchor_seq) if anchor_seq is not None else None
            before_y = anchor_w.virtual_region.y if anchor_w is not None else None
            before_scroll = pane.scroll_y

            for w in hidden:
                w.display = True
            hidden.clear()

            # Re-anchor: scroll by however far the anchor moved down, so the
            # prepended chunks extend the scrollable region UPWARD without moving
            # the user's view — continuous scroll instead of a wall.
            if anchor_w is not None and before_y is not None and anchor_seq is not None:
                await self._app._preview.await_settled()
                # Re-resolve across the await. The freeze sweep can swap this
                # chunk's widget for its capture and remove the original while
                # we yield; the container check does not see that, and a
                # condemned widget's `virtual_region` yields a delta that
                # scrolls the pane somewhere the user never asked to be.
                anchor_w = container.chunk_widgets.get(anchor_seq)
                if anchor_w is not None and not is_live(anchor_w):
                    anchor_w = None
                if anchor_w is not None and self._app._preview.active is container:
                    delta = anchor_w.virtual_region.y - before_y
                    if delta > 0:
                        self._app._preview.begin_reconcile_scroll()
                        try:
                            pane.scroll_to(y=before_scroll + delta, animate=False, immediate=True)
                            self._app._diag_log(
                                f"scroll site=lazy_above y={before_scroll + delta:.0f} "
                                f"delta={delta}"
                            )
                        finally:
                            self._app._preview.end_reconcile_scroll()
        finally:
            # Cancellation or unexpected return: anything still in
            # ``hidden`` would otherwise stay invisible on the cached
            # container.
            for w in hidden:
                with contextlib.suppress(Exception):
                    w.display = True

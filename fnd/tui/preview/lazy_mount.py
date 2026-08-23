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
from fnd.tui.preview.warmth import WarmState
from fnd.tui.preview_scrollbar import MatchAwareScroll
from fnd.tui.widgets.markdown import FNDMarkdown

if TYPE_CHECKING:
    from fnd.query import FileChunk
    from fnd.tui.app import FNDApp
    from fnd.tui.widgets.preview_container import PreviewContainer

__all__ = ["LazyMounter"]


def _all_measured(widgets: list[Widget]) -> bool:
    """Whether every revealed widget has been laid out and has a height.

    A widget with height 0 has not contributed to the document yet, so its
    share of the prepend is still to come.
    """
    for w in widgets:
        try:
            if w.virtual_region.height <= 0:
                return False
        except Exception:
            continue  # gone: it will never contribute
    return True


class LazyMounter:
    """Owns the lazy-mount task and its debounce timer; one instance
    lives on the app for the session."""

    def __init__(self, app: FNDApp) -> None:
        self._app = app
        # In-flight lazy-mount task (driven by scroll). One at a time;
        # cleared on file switch alongside ``_preview_mount_task``.
        self.task: object | None = None
        #: The background downward fill, kept apart from ``task`` so it never
        #: blocks a fill the reader is waiting on.
        self.fill_task: object | None = None
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

        # A fully captured file fills the rest of itself DOWNWARD in the
        # background; upward is left to the scroll below, one compensated batch
        # at a time. An unattended above-fill jitters the viewport for as long
        # as it runs — the constraint the presenter states for its own fill.
        if (
            len(chunks) <= tuning.FULLWARM_MOUNT_MAX_CHUNKS
            and self._app._preview.file_warm_state(container.parent_doc_id) is WarmState.FULL
        ):
            # Its OWN handle: `self.task` gates the user-driven fills, and a
            # background fill holding it dropped every upward request for as
            # long as it ran — the reader hit a wall and had to scroll back
            # down to shake one loose.
            fill = self.fill_task
            if fill is None or fill.done():  # type: ignore[attr-defined]
                self.fill_task = asyncio.create_task(self.fill_all(container, chunks))
            # No return: the directional checks below are what serve the
            # reader, and the fill runs for 3.6s on a 727-chunk file.

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
                self._mount_batch(
                    container,
                    chunks,
                    start_idx=next_idx,
                    direction="below",
                    size=self._served_batch(container, chunks, next_idx, 1),
                )
            )
            return

        top_cover = _covering(scroll_y) or chunk_ranges[0]
        top_idx, top_chunk_y0, _ = top_cover
        prev_idx = top_idx - 1
        # Look ahead in CHUNKS as well as rows. A PDF chunk is 100-150 rows, so
        # a 30-row margin only fires once the reader is already at the edge and
        # the mount lands under them; a chunk of lead time means it lands while
        # there is still mounted content between them and the seam.
        approaching = top_idx - min(container.mounted_indices) <= tuning.LAZY_MOUNT_TRIGGER_CHUNKS
        if (
            prev_idx >= 0
            and prev_idx not in container.mounted_indices
            and ((scroll_y - top_chunk_y0) <= margin or approaching)
        ):
            # Sized by what is served: a warmed file clears a wall in one step
            # instead of three chunks at a time — one batch per scroll event,
            # never an unattended loop.
            self.task = asyncio.create_task(
                self._mount_batch(
                    container,
                    chunks,
                    start_idx=prev_idx,
                    direction="above",
                    size=self._served_batch(container, chunks, prev_idx, -1),
                )
            )

    async def fill_all(self, container: PreviewContainer, chunks: list[FileChunk]) -> None:
        """Mount every remaining chunk of a fully captured file.

        A warmed file promises scrolling with no build anywhere, and the
        windowed path cannot deliver that: the above-fill runs only when a
        scroll event fires the debounced check, so at the top of the mounted
        region there is no movement left to re-arm it and the user has to
        scroll DOWN to get anything more above. Measured on a 727-chunk PDF,
        mounting the lot costs 3.6s at 5ms a chunk and leaves PageUp at 119ms.

        Above before below: above is where the wall is.
        """
        import asyncio

        while self._app._preview.active is container:
            mounted = container.mounted_indices
            if len(mounted) >= len(chunks):
                return
            before = len(mounted)
            missing = [i for i in range(len(chunks)) if i not in mounted]
            if not missing:
                return
            # BELOW only. Inserting content above the viewport shoves it down
            # and the scroll can only re-pin it a layout later, so an unattended
            # above-fill jitters the reader for as long as it runs — measured at
            # 19 of 21 painted frames showing the wrong part of the document.
            # Scrolling up stays user-driven, one compensated batch per scroll.
            # Strictly AFTER the last mounted chunk. "Missing and above the top
            # of the window" is not the same thing: mount_chunk_into inserts in
            # document order, so filling an interior gap puts widgets ABOVE the
            # viewport while the below branch — which does no scroll adjustment,
            # on the assumption content grows downward — displaces the reader
            # once per batch. Interior gaps are left to the scroll, which knows
            # which side of the viewport they are on.
            below = [i for i in missing if i > max(mounted)] if mounted else missing
            if not below:
                return
            start = below[0]
            await self._mount_batch(
                container,
                chunks,
                start_idx=start,
                direction="below",
                size=self._served_batch(container, chunks, start, 1),
            )
            if len(container.mounted_indices) <= before:
                # A batch that mounts nothing would spin. Something declined the
                # mount — a condemned widget, a swapped container — and the
                # windowed path can pick the rest up on scroll.
                return
            await asyncio.sleep(0)

    def _served_batch(
        self, container: PreviewContainer, chunks: list[FileChunk], start: int, step: int
    ) -> int:
        """The larger batch only when this run really is served from the store.

        FULL is counted over the CAPTURABLE chunks, so a file of mostly
        flat-path pages reads FULL while most of it still has to be built —
        and the served size is ten times what a build batch can afford.
        """
        span = [start + step * i for i in range(tuning.LAZY_MOUNT_BATCH_SERVED)]
        seqs = [chunks[i].chunk_seq for i in span if 0 <= i < len(chunks)]
        if seqs and self._app._preview.all_captured(container.parent_doc_id, seqs):
            return tuning.LAZY_MOUNT_BATCH_SERVED
        return tuning.LAZY_MOUNT_BATCH

    async def _mount_batch(
        self,
        container: PreviewContainer,
        chunks: list[FileChunk],
        *,
        start_idx: int,
        direction: str,
        size: int | None = None,
    ) -> None:
        """Mount ``size`` chunks (default ``tuning.LAZY_MOUNT_BATCH``) starting
        at ``start_idx``, moving in ``direction``.

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

        batch = tuning.LAZY_MOUNT_BATCH if size is None else size
        if direction == "below":
            end = min(start_idx + batch, len(chunks))
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
        end = max(start_idx - batch, -1)
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
                # Re-checked here as well as in the mount loop: this awaits per
                # widget, and a query landing in that window detaches the
                # container — the reveal below then changes no geometry and the
                # growth claim would strand, to be spent on the next document.
                if self._app._preview.active is not container:
                    return
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

            # A served chunk knows exactly how many rows it occupies, so when
            # every prepended chunk was served the delta needs no settled
            # layout. Measuring it afterwards means awaiting one, and every
            # frame painted in that window shows the document at the wrong
            # offset: measured at 120ms and 760 rows for a 30-chunk prepend.
            # Claim the growth BEFORE revealing: the pane absorbs it as the
            # resulting layout lands, which needs no height known in advance
            # and so works for built chunks as well as served ones.
            # isinstance, not a typed query: a pane that is not ours simply
            # does not absorb and falls through to the settled re-anchor below,
            # where a typed query would raise on it instead.
            absorb_pane: MatchAwareScroll | None = None
            if hidden and isinstance(pane, MatchAwareScroll) and anchor_w is not None:
                # Anchored on the first chunk BELOW the prepend, at its position
                # now. The claim has to outlive this call: `display = True` lays
                # nothing out — the arrange happens on a later timer tick — so a
                # claim released before returning was never present when the
                # growth it claimed arrived.
                pane.absorb_anchor = (anchor_w, int(anchor_w.virtual_region.y))
                absorb_pane = pane

            if absorb_pane is not None:
                # Suspend painting across the reveal AND the layout it causes.
                # The compensation lands inside that layout, but the compositor
                # can still paint the pre-correction offset first — the reader
                # sees the document a batch-height out of place for one frame.
                # Batched, the first frame anyone sees is the corrected one.
                revealed = list(hidden)
                with self._app.batch_update():
                    for w in hidden:
                        w.display = True
                    hidden.clear()
                    # Held until the prepend has finished landing. The
                    # signal is the revealed widgets themselves: each is
                    # laid out — and so contributes its height — at its own
                    # pace, which is why the growth arrives in instalments
                    # 40-57ms apart, and why releasing on a timer released
                    # just before the last one and let it paint
                    # uncompensated. The quiet ticks that follow are for the
                    # absorb of that final instalment to be applied.
                    settled = 0
                    last = absorb_pane.absorb_anchor
                    for _ in range(tuning.LAZY_MOUNT_ABSORB_TICKS):
                        await asyncio.sleep(0.01)
                        now = absorb_pane.absorb_anchor
                        if now != last:
                            settled = 0
                            last = now
                            continue
                        settled += 1
                        if settled < tuning.LAZY_MOUNT_ABSORB_QUIET:
                            continue
                        if _all_measured(revealed):
                            break
                return

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
            # The claim is NOT released here: the layout it compensates has not
            # happened yet. It is replaced by the next above-batch, and dropped
            # on a query reset or a container swap.
            # Cancellation or unexpected return: anything still in
            # ``hidden`` would otherwise stay invisible on the cached
            # container.
            for w in hidden:
                with contextlib.suppress(Exception):
                    w.display = True

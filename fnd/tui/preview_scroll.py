"""Centralised preview scroll-to-match.

Single source of truth for where the preview pane is scrolled. Replaces
the scattered inline scroll sites whose overlapping, differing scrolls
raced (last-writer-wins). Every layout/mount event reconciles against
ONE anchor, so call order no longer changes the outcome.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from textual.containers import VerticalScroll
from textual.geometry import Region
from textual.widget import Widget

from fnd.matching import MatchSpec

if TYPE_CHECKING:
    from textual.widgets import DataTable

    from fnd.tui.line_buffer import LineBufferPreview
    from fnd.tui.widgets.markdown import FNDMarkdown


# Consecutive refreshes the content above the match must keep the same height
# before the scroll commits. One is not enough — layout arrives in bursts, so a
# single unchanged sample lands on a plateau mid-growth.
_ABOVE_STABLE_TICKS = 2
# Floor on the shared retry budget (30) below which the settle gate stops
# waiting and commits anyway. Without it a chunk whose layout keeps moving —
# a heavy table/fence page — could hold the scroll for the whole budget, and a
# match that lands late is worse than one that lands a few rows off: measured
# an 864ms tail before this bound.
#
# Deliberately tight. The gate needs three iterations to see _ABOVE_STABLE_TICKS
# consecutive stable heights, so four deferrals is one spare — anything more is
# latency the settle waits on, and _settled gates lazy-mount and prefetch as well
# as the scroll. A looser bound delayed both far enough to fail CI (a Reading
# View re-wrap, which re-lays out every chunk, and a prefetch waiting on settle).
_ABOVE_WAIT_FLOOR = 26

# Refreshes ``scroll_to_location`` keeps re-anchoring for once the layout has
# stopped changing. The re-wrap lands in bursts, so the position can look stable
# for a stretch and then jump — hence a tail rather than an early exit.
_RESTORE_TAIL_REFRESHES = 12
# Hard bound on the whole re-anchor loop, including the top-ups it takes while a
# mount is still in flight. ~1.5s at 60fps: longer than the slowest measured
# window mount, short enough that a wedged pipeline can't hold the loop open.
_RESTORE_HARD_CAP = 90


@dataclass(frozen=True, slots=True)
class ScrollAnchor:
    parent_id: str
    focus_chunk_seq: int
    intent: str = "first_match"  # or "chunk_top"
    context_fraction: float = 0.25
    # Smoothly animate the scroll instead of jumping. Set for between-match
    # navigation within the same file (restores the pre-lazy-load glide);
    # left False for a fresh file, where the reveal is an instant cut.
    animate: bool = False


@dataclass(frozen=True, slots=True)
class ViewportLocation:
    """A restorable reading position in the preview — the read-counterpart of
    a scroll target. ``locate()`` produces one; ``scroll_to_location()``
    consumes it (Memento). Structural previews use ``chunk_seq`` + ``offset``
    (rows into the chunk); flat previews use ``line`` (a logical, wrap-stable
    line index). ``kind`` says which fields are meaningful."""

    kind: str  # "structural" | "flat"
    chunk_seq: int = 0
    offset: int = 0
    line: int = 0


class ScrollStrategy(Protocol):
    def reconcile(
        self,
        anchor: ScrollAnchor,
        on_settled: Callable[[], None] | None = None,
        *,
        generation: int = 0,
        current_generation: Callable[[], int] | None = None,
    ) -> None: ...
    def locate(self) -> ViewportLocation | None: ...
    def scroll_to_location(
        self, location: ViewportLocation, on_done: Callable[[], None] | None = None
    ) -> None: ...


class _Once:
    """One-shot wrapper: calls the wrapped callback at most once. Used so a
    fire-once callback (e.g. the preview reveal) can be invoked defensively on
    an error path without risking a double call when the happy path already
    fired it. ``None`` wraps to a no-op."""

    __slots__ = ("_cb", "_fired")

    def __init__(self, cb: Callable[[], None] | None) -> None:
        self._cb = cb
        self._fired = False

    def __call__(self) -> None:
        if self._fired:
            return
        self._fired = True
        if self._cb is not None:
            self._cb()


class PreviewScrollController:
    """Owns the active anchor and whether it is authoritative (armed).

    arm()       — navigation sets the desired target.
    reconcile() — idempotently scroll to the target via the active
                  strategy; no-op when released.
    release()   — user took scroll control; stop reconciling.
    """

    def __init__(self, select_strategy: Callable[[], ScrollStrategy | None]) -> None:
        self._select_strategy = select_strategy
        self._anchor: ScrollAnchor | None = None
        self._armed = False
        # True once the armed anchor's scroll has committed. arm() clears it;
        # a committed reconcile() sets it. ``is_settling`` (armed & not settled)
        # is the window during which scroll-driven lazy mount must stay out of
        # the controller's way — see is_settling.
        self._settled = False
        # Monotonic navigation epoch. The single anchor stopped the many-inline-
        # scroll-sites race, but the strategy's retry chain reschedules itself
        # across refreshes, so rapid navigation spawns OVERLAPPING chains (each
        # pinned to a captured chunk seq) that all commit a scroll — last writer
        # wins. arm() bumps this; the active chain captures it and bails (no
        # scroll, no reschedule, no settled-flip) the moment it's superseded.
        self._generation = 0
        self._epoch = 0
        self._pending_settled: _Once | None = None
        # Outstanding reflow-restores. A restore re-applies its scroll across
        # many refreshes as the re-wrap lands, and unlike a reconcile it never
        # arms the anchor — so ``is_settling`` says nothing about it. See
        # is_restoring.
        self._restoring = 0
        # Restores finished since start-up. ``is_restoring`` alone cannot say
        # "the restore has landed": a caller that schedules the restore via
        # call_after_refresh sees False both BEFORE it starts and after it
        # ends, so waiting on the flag can pass without anything happening.
        # Compare this against a value read before the trigger instead.
        self._restores_completed = 0

    @property
    def is_armed(self) -> bool:
        return self._armed

    @property
    def is_settling(self) -> bool:
        """A navigation is still landing: armed but its scroll hasn't committed.
        Lazy-mount suppresses itself while this holds so it doesn't fight the
        controller mid-settle; once the reveal lands it clears and user scrolls
        (keyboard OR unfocused wheel) extend the mounted window again."""
        return self._armed and not self._settled

    @property
    def is_restoring(self) -> bool:
        """A reflow-restore is still re-applying its scroll. Widening the pane
        (Reading View, a resize) re-wraps the content over an unknown number of
        refreshes, so the restore keeps re-scrolling until the layout stops
        moving. Anything that needs the final reading position — a measurement,
        a test assertion — must wait for this to clear; ``is_settling`` does not
        cover it, because a restore never arms the anchor."""
        return self._restoring > 0

    @property
    def restores_completed(self) -> int:
        """How many reflow-restores have finished. Monotonic, so a waiter can
        read it before triggering a restore and wait for it to exceed that —
        the only way to tell "not started yet" from "already landed"."""
        return self._restores_completed

    @property
    def anchor(self) -> ScrollAnchor | None:
        return self._anchor

    @property
    def generation(self) -> int:
        return self._generation

    def arm(self, anchor: ScrollAnchor) -> None:
        self._generation += 1  # newest navigation wins; older chains self-cancel
        self._epoch += 1
        self._pending_settled = None
        self._anchor = anchor
        self._armed = True
        self._settled = False

    def release(self) -> None:
        self._armed = False

    def reconcile(self, on_settled: Callable[[], None] | None = None) -> None:
        # ``on_settled`` fires EXACTLY ONCE once the scroll has committed (or
        # immediately when there is nothing to scroll). Cold/warm reveal paths
        # pass the container un-hide here so it is revealed AFTER the scroll
        # lands — never before — which keeps the match from flashing at the file
        # top then jumping. The contract is fire-once: a dropped call strands
        # the container hidden; a double call would re-run a reveal the callback
        # assumes happens once. We hand the strategy a one-shot latch, so even
        # if the strategy calls it AND then raises, the error-path call below is
        # a no-op. The latch guarantees the floor (fires on error) and the
        # ceiling (never twice).
        if on_settled is not None:
            self._pending_settled = _Once(on_settled)
        latch = self._pending_settled
        gen = self._generation  # this commit belongs to the current navigation
        self._epoch += 1
        epoch = self._epoch

        def fire() -> None:
            if epoch != self._epoch and gen == self._generation:
                return
            # The scroll has committed. Honour the one-shot reveal either way (a
            # dropped call strands the container hidden) — but only flip
            # ``_settled`` (opening the lazy-mount gate) when this is STILL the
            # current navigation. A superseded chain landing late must not open
            # the gate for the newer nav still in flight.
            if gen == self._generation:
                self._settled = True
            if latch is not None:
                latch()

        if not self._armed or self._anchor is None:
            fire()
            return
        strategy = self._select_strategy()
        if strategy is None:
            fire()
            return
        try:
            strategy.reconcile(
                self._anchor, fire, generation=epoch, current_generation=lambda: self._epoch
            )
        except Exception:
            fire()
            raise

    def locate(self) -> ViewportLocation | None:
        """Read the viewport's current top position — the counterpart to
        scrolling to one. Survives a width reflow (e.g. toggling Reading View
        re-wraps the content). Delegates to the active strategy; pass the
        result straight back to :meth:`scroll_to_location`. Best-effort: a
        failure returns None (position simply isn't restored) rather than
        breaking the surrounding UI action (e.g. the reading-mode toggle)."""
        strategy = self._select_strategy()
        if strategy is None:
            return None
        try:
            return strategy.locate()
        except Exception:
            return None

    def scroll_to_location(self, location: ViewportLocation | None) -> None:
        """Scroll to a position previously read by :meth:`locate`. Best-effort:
        a failure is swallowed (position just isn't restored) so it can't
        propagate into a UI event handler. ``is_restoring`` holds until the
        strategy reports the restore finished."""
        if location is None:
            return
        strategy = self._select_strategy()
        if strategy is None:
            return
        self._restoring += 1
        done = _Once(self._restore_done)
        try:
            strategy.scroll_to_location(location, done)
        except Exception:
            done()  # never strand the flag on a raising strategy

    def _restore_done(self) -> None:
        self._restoring = max(0, self._restoring - 1)
        self._restores_completed += 1


class StructuralHost(Protocol):
    """The slice of FNDApp the structural scroll strategy reads."""

    def preview_pane(self) -> VerticalScroll: ...
    def effective_match_spec(self) -> MatchSpec: ...
    def begin_reconcile_scroll(self) -> None: ...
    def end_reconcile_scroll(self) -> None: ...
    def swap_reveal_target(
        self, target: Widget, margin: int, anchor_region: Region | None = None
    ) -> bool: ...
    def call_after_refresh(
        self, callback: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> object: ...
    def diag_log(self, msg: str) -> None: ...
    def above_window_pending(self, focus_chunk_seq: int) -> bool: ...
    def pipeline_busy(self) -> bool: ...

    @property
    def chunk_widgets(self) -> dict[int, Widget]: ...
    @property
    def match_targets(self) -> dict[int, Widget]: ...


class StructuralScrollStrategy:
    """Scroll the structural (per-chunk ``FNDMarkdown``) preview to a match.

    Verbatim port of the former ``FNDApp._do_scroll_to_chunk`` /
    ``_scroll_proxy_for`` / ``_fallback_match_target`` /
    ``_scroll_pane_to_table_cell`` / ``_scroll_pane_to_match_region``. Reads
    what it needs through :class:`StructuralHost` so the scroll math is
    testable without the full TUI.
    """

    def __init__(self, host: StructuralHost) -> None:
        self._host = host

    def reconcile(
        self,
        anchor: ScrollAnchor,
        on_settled: Callable[[], None] | None = None,
        *,
        generation: int = 0,
        current_generation: Callable[[], int] | None = None,
        above_height: int | None = None,
        stable_ticks: int = 0,
    ) -> None:
        from fnd.tui.widgets.markdown import FNDMarkdown

        seq = anchor.focus_chunk_seq
        header = self._host.chunk_widgets.get(seq)
        if header is None:
            if on_settled is not None:
                on_settled()
            return
        # Move the focused-section accent band to the target chunk (FNDMarkdown
        # manages its own focus highlight internally, so skip the band there).
        #
        # A frozen chunk is skipped for a different reason: the band paints the
        # widget's BACKGROUND, and a capture's strips are opaque, so the band
        # can never tint the content. All it can reach is the one padding row
        # the strips do not cover — which is the stray amber bar sitting above
        # the text. It was invisible while a stand-in was sized to its strips
        # alone, and appeared the moment the stand-in started carrying the
        # padding it is supposed to have.
        from fnd.tui.preview.frozen import FrozenChunkView

        for w in self._host.chunk_widgets.values():
            w.remove_class("chunk-section-focused")
        if not isinstance(header, FNDMarkdown | FrozenChunkView):
            header.add_class("chunk-section-focused")
        self._host.call_after_refresh(
            self._do_scroll_to_chunk,
            seq,
            30,
            on_settled,
            anchor.context_fraction,
            anchor.animate,
            generation,
            current_generation,
        )

    def _superseded(self, generation: int, current_generation: Callable[[], int] | None) -> bool:
        """This scroll chain was started for ``generation`` but a newer
        navigation has since bumped the controller's generation — so it must not
        scroll or reschedule (last-writer-wins avoidance)."""
        return current_generation is not None and generation != current_generation()

    def _do_scroll_to_chunk(
        self,
        focus_chunk_seq: int,
        retries: int = 30,
        on_done: Callable[[], None] | None = None,
        margin_from: float = 0.25,
        animate: bool = False,
        generation: int = 0,
        current_generation: Callable[[], int] | None = None,
        above_height: int | None = None,
        stable_ticks: int = 0,
    ) -> None:
        from fnd.tui.widgets.markdown import FNDMarkdown

        # Generation guard (entry): a superseded retry chain dies on its next
        # tick — no scroll, no reschedule — but still fires on_done so the
        # one-shot reveal floor holds (the reveal itself is identity-guarded).
        if self._superseded(generation, current_generation):
            if on_done is not None:
                on_done()
            return
        # Resolve target at fire time: FNDMarkdown.first_match_block
        # is populated async by build_from_token, so capturing earlier
        # races the build and lands on chunk top.
        header = self._host.chunk_widgets.get(focus_chunk_seq)
        if header is None:
            self._host.diag_log(f"do_scroll seq={focus_chunk_seq} miss=no-header")
            if on_done is not None:
                on_done()
            return
        target: Widget = self._host.match_targets.get(focus_chunk_seq) or header
        path = "match_targets" if focus_chunk_seq in self._host.match_targets else "header"
        fallback_fired = False
        # A frozen chunk lands via its captured row, not a match block, so
        # without this it forfeits the context margin below and puts the match
        # on the viewport's top line.
        first_match_seen = getattr(target, "fnd_first_match_row", None) is not None
        chunk_md = target if hasattr(target, "first_match_block") else None
        if chunk_md is not None:
            inner = chunk_md.first_match_block  # pyright: ignore[reportAttributeAccessIssue]
            # Retry only while the build could still produce a match.
            # ``first_match_block`` is populated during build_from_token, so
            # before the build finishes ``None`` means "not yet"; after it, it
            # means "there is no match in this chunk" and no amount of
            # refreshing will change that. Waiting out all 30 refreshes on a
            # genuinely match-free chunk delayed the reveal for nothing.
            build_done = getattr(chunk_md, "build_done", None)
            still_building = build_done is None or not build_done.is_set()
            if inner is None and retries > 0 and still_building:
                self._host.call_after_refresh(
                    self._do_scroll_to_chunk,
                    focus_chunk_seq,
                    retries - 1,
                    on_done,
                    margin_from,
                    animate,
                    generation,
                    current_generation,
                )
                return
            if inner is not None:
                first_match_seen = True
                target = (
                    self._scroll_proxy_for(inner, chunk=chunk_md)
                    if isinstance(chunk_md, FNDMarkdown)
                    else inner
                )
                path = f"first_match_block({type(inner).__name__})"
            else:
                # first_match_block never resolved; descend into the chunk
                # for any widget whose text carries the query.
                fallback_fired = True
                target = (
                    self._fallback_match_target(chunk_md)
                    if isinstance(chunk_md, FNDMarkdown)
                    else chunk_md
                )
                landed_on_chunk = target is chunk_md
                self._host.diag_log(
                    f"do_scroll seq={focus_chunk_seq} fallback=descendant-scan "
                    f"result={'chunk-top' if landed_on_chunk else type(target).__name__} "
                    f"retries_left={retries}"
                )
                path = f"fallback({type(target).__name__})"
        # Content ABOVE the match decides where it ends up on screen, and it is
        # still arriving when the scroll first runs: chunk widgets mount at ~0
        # height and grow as their markdown lays out. Committing early lands
        # correctly and then slides — ~18 rows on the AWS guide's p.24, leaving
        # the match two thirds down the pane instead of a quarter.
        #
        # Two distinct things have to settle, and neither implies the other:
        #
        # * the window's chunks have to EXIST. Navigating backwards into a file
        #   mounts them after this first runs, so inspecting only what is
        #   currently mounted sees nothing pending (the trap
        #   ``_finalise_via_lock``'s ``expected_above_seqs`` exists to avoid);
        # * their HEIGHT has to stop changing. ``build_done`` is not that
        #   signal — measured on p.24, the seven chunks above were all mounted
        #   with build_done set and still grew 142 → 159 rows afterwards.
        #
        # So: ask the host about existence, and watch the measured height until
        # it holds still for two consecutive refreshes. A settled file passes
        # both on the first look and pays two refresh ticks; the alternative is
        # landing in the wrong place.
        if retries > 0:
            above = [w for s, w in self._host.chunk_widgets.items() if s < focus_chunk_seq]
            measured = sum(w.region.height for w in above)

            def _wait(ticks: int) -> None:
                self._host.call_after_refresh(
                    self._do_scroll_to_chunk,
                    focus_chunk_seq,
                    retries - 1,
                    on_done,
                    margin_from,
                    animate,
                    generation,
                    current_generation,
                    measured,
                    ticks,
                )

            if self._host.above_window_pending(focus_chunk_seq):
                # Content that will sit above the match doesn't exist yet, so
                # its position is about to change. Wait — bounded only by the
                # shared retry budget, like every other "not laid out yet"
                # condition here. Bounding this more tightly gave up on a slow
                # mount and landed the match off screen entirely.
                _wait(0)
                return
            if above and retries >= _ABOVE_WAIT_FLOOR:
                if any(w.region.height == 0 for w in above):
                    # An unlaid-out chunk measures 0 and keeps measuring 0, so
                    # the stability check reads three zeroes as "settled" and
                    # commits — then the chunk lays out and pushes the match
                    # down. Zero height is the absence of a measurement, not a
                    # stable one. The floor still bounds this, so a genuinely
                    # empty chunk can't stall navigation.
                    _wait(0)
                    return
                # Mounted and built; now wait for the measured height to hold
                # still, since build_done is not a height-settled signal. THIS
                # is what the floor bounds — a page whose layout never quite
                # stops moving still has to land.
                ticks = stable_ticks + 1 if measured == above_height else 0
                if ticks < _ABOVE_STABLE_TICKS:
                    _wait(ticks)
                    return
        if target.region.height == 0 and retries > 0:
            self._host.call_after_refresh(
                self._do_scroll_to_chunk,
                focus_chunk_seq,
                retries - 1,
                on_done,
                margin_from,
                animate,
                generation,
                current_generation,
            )
            return
        if target.region.height == 0:
            self._host.diag_log(
                f"do_scroll seq={focus_chunk_seq} miss=zero-region "
                f"target={type(target).__name__} path={path}"
            )
        # A match inside a table renders as one full-height DataTable (no
        # per-cell widgets, no internal scroll), so the matched cell is not its
        # own widget — resolve the cell's region as the scroll anchor instead of
        # the table's own (which is the table top). While the rows are still
        # mounting the cell region isn't laid out; retry rather than committing a
        # scroll to the table top — the race that stranded deep-table matches at
        # the top on a cold mount. ``_anchor_region`` returns ``target.region``
        # unchanged for non-table targets.
        match_table = self._match_table_for(target)
        anchor = self._anchor_region(target, match_table)
        if anchor is None and retries > 0:
            self._host.call_after_refresh(
                self._do_scroll_to_chunk,
                focus_chunk_seq,
                retries - 1,
                on_done,
                margin_from,
                animate,
                generation,
                current_generation,
            )
            return
        if anchor is None:
            # Retries exhausted with the cell still unresolved — fall back to the
            # matched DataTable's own region (its top), logged so the regression
            # is visible. anchor is None only in the table branch, so match_table
            # is set; the wrapper target.region can be a zero-height/offset region.
            self._host.diag_log(
                f"do_scroll seq={focus_chunk_seq} miss=table-cell-unresolved "
                f"target={type(target).__name__} path={path}"
            )
            anchor = match_table.region if match_table is not None else target.region
        if match_table is None:
            from fnd.tui.preview.match_row import region_at_row

            anchor = region_at_row(anchor, self._match_line_offset(target))
        # Generation guard (immediately before the commit): the resolution above
        # spanned refreshes, during which a newer navigation may have superseded
        # this chain. Re-check freshness right before the side effect — the
        # cooperative-cancellation rule. A superseded chain bails without
        # scrolling (on_done still fires the reveal floor; identity-guarded).
        if self._superseded(generation, current_generation):
            if on_done is not None:
                on_done()
            return
        try:
            pane = self._host.preview_pane()

            # Drop the match ~a quarter down the viewport so the lines above it
            # give context, instead of pinning it to the top line — but only when
            # we actually landed on a match (not a bare chunk-top navigation).
            margin = (
                int(pane.size.height * margin_from) if (first_match_seen or fallback_fired) else 0
            )
            # Flag this as the controller's own scroll so the resulting scroll-
            # watcher trip isn't mistaken for a user scroll and doesn't self-release
            # the anchor.
            unscrollable = False
            defer_note = ""
            self._host.begin_reconcile_scroll()
            try:
                # If an outgoing preview is being held on screen, hand the
                # resolved anchor to the host so it can hide the old one, position
                # this one, and reveal it in a single tick (no blank between
                # previews). The anchor (screen space) is the matched table cell
                # for a table match, so the swap lands on the matched row too —
                # not the table top. When there is no outgoing container this is a
                # no-op and we scroll the already-visible pane normally.
                if self._host.swap_reveal_target(target, margin, anchor):
                    pass
                else:
                    # Map the anchor's screen region into the pane's scrollable
                    # content space and scroll there in one shot. (Reading
                    # scroll_offset back after scroll_to_widget to apply the margin
                    # races a cold render — the offset isn't committed yet, so the
                    # nudge lands on a stale, wrong position.)
                    region = anchor.translate(
                        pane.scroll_offset - pane.scrollable_content_region.offset
                    )
                    # ``scroll_to_region`` clamps its delta against ``max_scroll_y``,
                    # which is 0 until the pane recomputes ``virtual_size`` for the
                    # laid-out content: the scroll lands nowhere and reports success.
                    # ``focus_chunk_seq`` 0 arrives with no settle spent — every gate
                    # above needs chunks ABOVE the match, and there are none.
                    unscrollable = (
                        retries > 0 and region.y + region.height > pane.virtual_size.height
                    )
                    if unscrollable:
                        defer_note = (
                            f"region_bottom={region.y + region.height} "
                            f"vsize_h={pane.virtual_size.height}"
                        )
                    else:
                        self._scroll_pane_to_match_region(pane, region, margin, animate=animate)
            finally:
                self._host.end_reconcile_scroll()
            if unscrollable:
                # Carry the settle gate's accumulated state, as `_wait` does:
                # dropping it restarts the height-stability counter and pays
                # refreshes re-establishing what this call already knew.
                self._host.call_after_refresh(
                    self._do_scroll_to_chunk,
                    focus_chunk_seq,
                    retries - 1,
                    on_done,
                    margin_from,
                    animate,
                    generation,
                    current_generation,
                    above_height,
                    stable_ticks,
                )
                # The suite never reaches this path (A/B against main is
                # identical), so a field trace is the only way this guard is
                # ever confirmed or refuted.
                self._host.diag_log(
                    f"do_scroll seq={focus_chunk_seq} defer=unscrollable "
                    f"{defer_note} retries_left={retries - 1}"
                )
                on_done = None  # the retried chain owns the reveal
                return
            self._host.diag_log(
                f"do_scroll seq={focus_chunk_seq} target={type(target).__name__} "
                f"path={path} first_match={first_match_seen} fallback={fallback_fired} "
                f"retries_used={30 - retries}"
            )
        except Exception as _scroll_err:
            self._host.diag_log(
                f"do_scroll seq={focus_chunk_seq} error={type(_scroll_err).__name__}: {_scroll_err}"
            )
        finally:
            if on_done is not None:
                on_done()

    def _scroll_pane_to_match_region(
        self, pane: VerticalScroll, region: Region, margin: int, *, animate: bool = False
    ) -> None:
        """Scroll ``pane`` so ``region`` (already in the pane's scrollable-
        content space) sits ``margin`` rows down from the top, giving the match
        some context above it. One ``scroll_to_region`` call — no reading the
        offset back, so nothing races a cold render's deferred layout.
        ``animate`` glides the scroll (between-match nav within a file) instead
        of jumping (a fresh file's reveal, which lands instantly)."""
        if margin:
            region = Region(
                region.x, max(0, region.y - margin), region.width, region.height + margin
            )
        pane.scroll_to_region(region, top=True, animate=animate, immediate=not animate)
        self._host.diag_log(f"scroll site=match region_y={region.y} animate={animate}")

    def _match_table_for(self, target: Widget) -> DataTable[Any] | None:
        """The match-bearing ``DataTable`` ``target`` is or wraps, else None.

        ``target`` may be the DataTable itself or the ``FNDMarkdownTableDT``
        wrapper (the match scroll resolves to the wrapper when the
        first_match_block is a phantom, never-mounted TD cell). A table without
        a ``_fnd_match_coord`` is treated as a plain widget (returns None) so it
        scrolls to its own region."""
        from textual.widgets import DataTable

        from fnd.tui.widgets.markdown import FNDMarkdownTableDT

        if isinstance(target, DataTable):
            table = target
        elif isinstance(target, FNDMarkdownTableDT):
            table = next((c for c in target.query(DataTable)), None)
        else:
            return None
        if table is None or getattr(table, "_fnd_match_coord", None) is None:
            return None
        return table

    def _anchor_region(self, target: Widget, table: DataTable[Any] | None) -> Region | None:
        """Screen-space region the scroll should land on.

        For a plain widget that's its own ``region``. For a match inside a
        table — one full-height DataTable with no per-cell widgets and no
        internal scroll — it's the matched *cell's* region, so a match in a
        lower row is actually revealed instead of the table top.

        Returns ``None`` when ``target`` is a table whose cell region is not
        resolvable yet (rows unmounted / not sized — ``_get_cell_region`` raises
        or yields a zero-height region). The caller retries rather than
        committing a scroll to the table top, which is the race that stranded
        deep-table matches at the top after a cold mount."""
        if table is None:
            return target.region
        coord = table._fnd_match_coord  # pyright: ignore[reportAttributeAccessIssue]
        try:
            cell = table._get_cell_region(coord)  # pyright: ignore[reportAttributeAccessIssue]
        except Exception:
            return None
        if cell.height == 0:
            return None
        # cell is relative to the table's content; map → screen (the table has
        # no internal scroll, but honour its offset defensively).
        return cell.translate(table.region.offset - table.scroll_offset)

    def _match_line_offset(self, target: Widget) -> int:
        """Rows from ``target``'s top down to its first match's row, taken from
        the capture's recorded row when the widgets it was measured from are
        gone (see :mod:`fnd.tui.preview.match_row`)."""
        frozen_row = getattr(target, "fnd_first_match_row", None)
        if isinstance(frozen_row, int):
            return frozen_row if 0 < frozen_row < target.region.height else 0
        from fnd.tui.preview.match_row import rows_to_first_match

        return rows_to_first_match(target, self._host.effective_match_spec())

    def _fallback_match_target(self, chunk: FNDMarkdown) -> Widget:
        """Scan ``chunk``'s descendants for the first widget whose plain text
        contains a match. Used when no highlight-aware subclass claimed
        ``first_match_block`` (e.g. matches inside a MarkdownFence)."""
        spec = self._host.effective_match_spec()
        if spec.is_empty:
            return chunk
        from fnd.render import text_has_any_match
        from fnd.tui.preview.match_row import block_plain

        for w in chunk.query("*"):
            if w is chunk:
                continue
            plain = block_plain(w)
            if plain and text_has_any_match(plain, spec) and w.region.height > 0:
                return w
        return chunk

    def _scroll_proxy_for(self, inner: Widget, *, chunk: FNDMarkdown) -> Widget:
        """Resolve a scroll target for an ``FNDMarkdown.first_match_block``.

        Most blocks (Paragraph / H#, ListItem, BlockQuote) have valid
        regions — use them directly. Table cells (TH/TD) carry the
        highlight bookkeeping but never get laid out: the parent
        ``MarkdownTable`` composes a ``MarkdownTableContent`` whose
        ``MarkdownTableCellContents`` children render in a grid. For
        that case, find the cell widget that holds the matched
        ``Content`` and scroll to it directly. Bounded by the number
        of cells in the chunk's tables — no full descendant walk.
        """
        from fnd.tui.widgets.markdown import FNDMarkdownTableDT

        # W3 path: the inner is the FNDMarkdownTableDT itself (which
        # registered itself as first_match_block). Scroll the DataTable
        # to the matched cell so the user lands on the actual match.
        if isinstance(inner, FNDMarkdownTableDT):
            from textual.widgets import DataTable

            for child in inner.children:
                if isinstance(child, DataTable):
                    coord = getattr(child, "_fnd_match_coord", None)
                    if coord is not None:
                        with contextlib.suppress(Exception):
                            child.move_cursor(row=coord.row, column=coord.column, scroll=True)
                    return child
            return inner
        if inner.region.height > 0:
            return inner
        from textual.widgets._markdown import MarkdownTable, MarkdownTableContent

        target_content = getattr(inner, "_content", None)
        if target_content is None:
            return chunk
        target_plain = getattr(target_content, "plain", None)
        # Remember the first MarkdownTable in document order as the
        # fallback: if cell-level lookup misses (Textual internals
        # vary), at least scrolling to the table itself is closer than
        # the chunk top.
        first_table: Widget | None = None
        for child in chunk.children:
            if not isinstance(child, MarkdownTable):
                continue
            if first_table is None and child.region.height > 0:
                first_table = child
            tcontent: MarkdownTableContent | None = None
            for grand in child.children:
                if isinstance(grand, MarkdownTableContent):
                    tcontent = grand
                    break
            if tcontent is None:
                continue
            for cell in tcontent.children:
                cell_content = getattr(cell, "content", None)
                if cell_content is target_content:
                    return cell if cell.region.height > 0 else child
                if (
                    target_plain
                    and cell_content is not None
                    and getattr(cell_content, "plain", None) == target_plain
                ):
                    return cell if cell.region.height > 0 else child
        return first_table or chunk

    def locate(self) -> ViewportLocation | None:
        """The chunk at the viewport top + how far into it the top sits.
        Survives a width reflow at chunk granularity: re-wrapping changes a
        chunk's height, but the chunk's content position is found again."""
        pane = self._host.preview_pane()
        top = pane.scrollable_content_region.y
        for seq, w in self._host.chunk_widgets.items():
            r = w.region
            if r.height > 0 and r.y <= top < r.y + r.height:
                return ViewportLocation("structural", chunk_seq=seq, offset=top - r.y)
        return None

    def scroll_to_location(
        self, location: ViewportLocation, on_done: Callable[[], None] | None = None
    ) -> None:
        done = _Once(on_done)
        if location.kind != "structural":
            done()
            return
        self._restore_structural(
            location.chunk_seq,
            location.offset,
            retries=_RESTORE_TAIL_REFRESHES,
            last_vy=None,
            done=done,
        )

    def _restore_structural(
        self,
        seq: int,
        delta: int,
        retries: int,
        last_vy: int | None,
        done: Callable[[], None],
        cap: int = _RESTORE_HARD_CAP,
    ) -> None:
        # A width reflow re-wraps the chunks above over several refreshes,
        # which keeps sliding the target chunk's content position. Track that
        # content position (``virtual_region.y`` — scroll-independent) and
        # re-apply until it stops moving; watching ``scroll_y`` instead settles
        # early (the offset repeats while the layout is still flowing) and
        # lands a chunk off. Scroll numerically to the chunk's content top plus
        # the captured in-chunk offset.
        w = self._host.chunk_widgets.get(seq)
        if w is None:
            done()
            return
        pane = self._host.preview_pane()
        vy = w.virtual_region.y
        # Flag as the controller's own scroll so the watcher trip isn't read
        # as a user scroll (which would self-release the anchor).
        self._host.begin_reconcile_scroll()
        try:
            pane.scroll_to(y=max(0, vy + delta), animate=False, immediate=True)
            self._host.diag_log(f"scroll site=restore y={max(0, vy + delta)} retries={retries}")
        finally:
            self._host.end_reconcile_scroll()
        # The width reflow re-wraps the chunks above asynchronously over many
        # refreshes, so the chunk's content position keeps moving after we
        # first scroll. Re-apply on every refresh for the whole budget (do NOT
        # early-stop: the position is stale-stable for a stretch, then jumps
        # once the re-wrap lands), re-reading it each time so the final applies
        # land on the settled layout.
        #
        # A fixed refresh count is only a PROXY for "until the layout stops
        # moving", and it breaks the moment the layout moves for longer than the
        # proxy allows: a mount still in flight keeps adding chunks above this
        # one, so the restore ran out of refreshes and left the target off
        # screen. Top the budget back up while the preview pipeline is still
        # working, so the tail refreshes are spent on a layout that has actually
        # stopped changing. Bounded by ``cap`` — a wedged pipeline must not hold
        # a re-anchor loop open forever.
        if cap > 0 and self._host.pipeline_busy():
            retries = max(retries, _RESTORE_TAIL_REFRESHES)
        if retries > 0 and cap > 0:
            self._host.call_after_refresh(
                self._restore_structural, seq, delta, retries - 1, vy, done, cap - 1
            )
        else:
            # Exhausting the budget (or the cap) still ENDS the restore — the
            # flag must not outlive the loop that set it.
            done()


def stop_region_for_cell(table: DataTable[Any], coord: Any) -> Region | None:
    """Screen-space region of a DataTable cell, or ``None`` if unresolved
    (rows unmounted / not sized). Mirrors ``StructuralScrollStrategy.
    _anchor_region``: the full-height table has no internal scroll, but honour
    its offset defensively."""
    try:
        cell = table._get_cell_region(coord)  # pyright: ignore[reportAttributeAccessIssue]
    except Exception:
        return None
    if cell.height == 0:
        return None
    return cell.translate(table.region.offset - table.scroll_offset)


def enumerate_stop_regions(pane: VerticalScroll, spec: MatchSpec) -> list[Region]:
    """Every match stop's screen-space region across the pane's mounted
    chunks, sorted by content-space y. Tables expand to one region per matching
    cell (``_fnd_match_coords``); other ``FNDMarkdown`` blocks contribute one
    region each; plain (pdf/txt) chunks contribute one per matching body line.

    Off-screen cells of a mounted table resolve fine — the table is one
    full-height widget — so a big flashcards/glossary table is fully covered
    once its chunk is mounted. Queries descendants (chunks live inside a
    ``PreviewContainer``), not just the pane's direct children.

    A FROZEN chunk has no blocks to walk — that is the point of freezing — so its
    stops come from the rows recorded at capture time. Without this a frozen
    chunk contributes nothing, and its matches become unreachable by ``n``/``b``
    and invisible to the off-screen markers: the failure is silent, which is
    exactly why it is handled here rather than left to the caller."""
    from textual.widgets import DataTable

    from fnd.render import text_has_any_match
    from fnd.tui.preview.frozen import FrozenChunkView
    from fnd.tui.preview.match_row import region_at_row, rows_to_first_match
    from fnd.tui.widgets.markdown import (
        FNDMarkdown,
        FNDMarkdownTableDT,
        FNDMarkdownTD,
        FNDMarkdownTH,
    )

    regions: list[Region] = []
    if spec.is_empty:
        return regions
    for md in pane.query(FNDMarkdown):
        # Tables: query the DataTable directly for every matching cell. (The
        # table's TH/TD cells also self-register in match_blocks as phantom,
        # never-mounted blocks — skip them below; the table owns their cells.)
        for dt in md.query(DataTable):
            for coord in getattr(dt, "_fnd_match_coords", []):
                r = stop_region_for_cell(dt, coord)
                if r is not None:
                    regions.append(r)
        # Non-table match blocks, on the row the match PAINTS on: a stop on the
        # block's top row sends n/b, and the ▲▼ markers that read this, to a row
        # with no match on it.
        for block in md.match_blocks:
            if isinstance(block, FNDMarkdownTableDT | FNDMarkdownTD | FNDMarkdownTH):
                continue
            if block.region.height > 0:
                regions.append(region_at_row(block.region, rows_to_first_match(block, spec)))
    # Frozen chunks: the stops were recorded as rows while the blocks still
    # existed, so they resolve by offset from the view's own top. A table cell's
    # row came from the same capture, which is why they need no special case
    # here — unlike the live path above, where a cell has to be resolved against
    # a DataTable that may not have laid its rows out yet.
    for view in pane.query(FrozenChunkView):
        base = view.region
        if base.height == 0:
            continue
        for row in view.frozen.stop_rows:
            if 0 <= row < base.height:
                regions.append(Region(base.x, base.y + row, base.width, 1))
    # Plain (pdf/txt) chunks render one Static per body line; a matching line
    # is a stop.
    for line in pane.query("Static.chunk-line"):
        txt = getattr(line, "fnd_text", None)
        if txt and text_has_any_match(txt, spec) and line.region.height > 0:
            regions.append(line.region)
    # Content-space y so the order is stable regardless of the live scroll.
    regions.sort(key=lambda r: r.y + pane.scroll_offset.y)
    return regions


class FlatHost(Protocol):
    """The slice of FNDApp the flat scroll strategy needs."""

    def active_flat_buffer(self) -> LineBufferPreview | None: ...


class FlatScrollStrategy:
    """Scroll the flat line-buffer preview to a match.

    The widget owns the row math (:class:`~fnd.tui.strip_document.StripDocumentView`);
    this strategy only hands it the target chunk and the context margin. The
    dispatch re-arms the anchor with the resolved focus chunk, so the widget's
    own first-match / chunk-top fallback handles the rest.

    Deliberately tiny next to :class:`StructuralScrollStrategy`: the buffer is
    one widget that already knows every row, so the scroll is synchronous — no
    retry chain, no settle barrier, nothing to wait on a build for.
    """

    def __init__(self, host: FlatHost) -> None:
        self._host = host
        self._view = host.active_flat_buffer

    def reconcile(
        self,
        anchor: ScrollAnchor,
        on_settled: Callable[[], None] | None = None,
        *,
        generation: int = 0,
        current_generation: Callable[[], int] | None = None,
    ) -> None:
        # A document scroll is synchronous within one reconcile (no retry
        # chain), so a single entry guard is enough: a superseded call doesn't
        # move the view.
        if current_generation is not None and generation != current_generation():
            if on_settled is not None:
                on_settled()
            return
        view = self._view()
        if view is None:
            if on_settled is not None:
                on_settled()
            return
        view.scroll_to_chunk(
            anchor.focus_chunk_seq,
            prefer_first_match=True,
            context_fraction=anchor.context_fraction,
        )
        # The view scrolls synchronously, so it has already landed — reveal
        # immediately.
        if on_settled is not None:
            on_settled()

    def locate(self) -> ViewportLocation | None:
        """The address at the viewport top — exact across a width reflow, which
        a raw visual row would not be (the flat buffer re-wraps; a frozen
        document re-freezes)."""
        view = self._view()
        if view is None:
            return None
        address = view.top_address()
        return None if address is None else ViewportLocation("flat", line=address)

    def scroll_to_location(
        self, location: ViewportLocation, on_done: Callable[[], None] | None = None
    ) -> None:
        done = _Once(on_done)
        try:
            if location.kind != "flat":
                return
            view = self._view()
            if view is None:
                return
            # Exact (no context margin) — restore the *reading* position, not a
            # match drop. The view re-renders for the new width first.
            view.scroll_to_address(location.line, context_fraction=0.0)
        finally:
            # The flat restore is synchronous: it is done the moment it returns.
            done()

"""Within-result preview match navigation (n / b).

The results-pane arrows step between results; within a result whose chunk is
taller than the viewport they skip straight past matches below the fold. This
module hops between those hidden matches by viewport — scoped to the current
result — and surfaces ``▲a ▼b`` view counts so the user knows they exist. The
geometry (next/prev stop, view bucketing) is pure and unit-tested; region
resolution + scrolling live on :class:`MatchNavigator` below.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.containers import VerticalScroll
    from textual.widget import Widget

    from fnd.matching import MatchSpec
    from fnd.tui.app import FNDApp


def _landing_margin(vh: int) -> int:
    """Rows a jumped-to match is dropped below the viewport top (a quarter down,
    ≥1). The SAME value drives the actual scroll (``_scroll_to_stop``) and the
    burst reference viewport (``_ref_top`` via the choosers) — they must agree or
    a burst hop on a non-16-row pane picks the wrong stop."""
    return max(1, int(vh * 0.25))


def _ref_top(ys: list[int], scroll_y: int, last_target: int | None, margin: int) -> int:
    """Top of the reference viewport: the last jump's resulting position during
    a burst (``last_target`` set), else the live scroll top. Using the last
    target's position — not the not-yet-settled live scroll — lets a rapid
    ``n n n`` burst advance screen-by-screen instead of re-picking the same
    stop while the animation catches up. A manual scroll clears ``last_target``
    (see :meth:`MatchNavigator.on_manual_scroll`), so it reverts to live."""
    if last_target is not None and 0 <= last_target < len(ys):
        return ys[last_target] - margin
    return scroll_y


def _view_buckets(ys: list[int], vh: int) -> int:
    """Number of viewport-sized screenfuls needed to cover ``ys`` (sorted) — a
    greedy sweep: each screenful spans ``vh`` from the first match it reaches, so
    matches within one viewport of each other count as one "view". This is the
    count of ``n``/``b`` hops it takes to visit them all."""
    if vh <= 0:
        return 0  # unlaid-out / hidden pane — no meaningful screenful width
    views = 0
    covered_to: int | None = None
    for y in ys:
        if covered_to is None or y >= covered_to:
            views += 1
            covered_to = y + vh
    return views


# Re-measures a navigation gets after its first, and the gap between them. Three
# at 150ms covers a reflow landing up to ~0.5s after the scroll did, which is
# what a loaded runner produces; the cap is what stops it becoming a poll.
_CONFIRMATIONS = 3
_CONFIRM_DELAY = 0.15


def offscreen_views(ys: list[int], top: int, bottom: int, vh: int) -> tuple[int, int]:
    """``(matching views above the viewport, matching views below)`` — the
    awareness signal behind the ``▲a ▼b`` border. ``ys`` are the current
    result's match stops (content-space tops, sorted); ``[top, bottom)`` is the
    viewport in the same space; ``vh`` is its height. A "view" is a screenful
    that holds ≥1 off-screen match, so ``▼2`` means two more screenfuls of this
    result lie below the fold. A stop exactly at ``bottom`` is below; one at
    ``top`` is visible."""
    above = [y for y in ys if y < top]
    below = [y for y in ys if y >= bottom]
    return _view_buckets(above, vh), _view_buckets(below, vh)


def next_stop_index(
    ys: list[int], scroll_y: int, viewport_h: int, last_target: int | None, margin: int
) -> int:
    """Index of the first stop at/below the reference viewport's bottom edge;
    wraps to 0 when none is below. ``ys`` is the stops' content-space tops,
    sorted ascending."""
    if not ys:
        return 0
    ref_bottom = _ref_top(ys, scroll_y, last_target, margin) + viewport_h
    for i, y in enumerate(ys):
        if y >= ref_bottom:
            return i
    return 0


def prev_stop_index(
    ys: list[int], scroll_y: int, viewport_h: int, last_target: int | None, margin: int
) -> int:
    """Index of the top stop of the screenful immediately above the reference
    viewport; wraps to the last stop when none is above."""
    if not ys:
        return 0
    ref_top = _ref_top(ys, scroll_y, last_target, margin)
    nearest_above: int | None = None
    for i, y in enumerate(ys):
        if y < ref_top:
            nearest_above = i
        else:
            break
    if nearest_above is None:
        return len(ys) - 1
    # Land on the TOP of that previous screenful (symmetry with next's hop),
    # not just the nearest stop above — so a screen with many matches is one
    # press up, mirroring one press down. Clamp the window so a tiny viewport
    # (viewport_h < 2*margin) doesn't invert it and break the grouping.
    window_top = ys[nearest_above] - max(0, viewport_h - 2 * margin)
    target = nearest_above
    for i in range(nearest_above, -1, -1):
        if ys[i] >= window_top:
            target = i
        else:
            break
    return target


class MatchNavigator:
    """Owns intra-file match navigation for the live preview pane.

    The three surfaces deliberately use DIFFERENT data so each stays correct,
    cheap, AND free of side effects on unrelated scroll timing:

    * **The footer-hint gate** (`count`) is derived from match DATA —
      ``_fnd_match_coords`` on the mounted tables + each chunk's ``match_blocks``
      — plain list attributes set at compose time. Counting them never reads a
      region, so it never forces layout, so it can't perturb the delicate cold-
      nav scroll-settle window (region reads did — measured to stall the
      landing). It is cached; the footer reads the cache, so a focus change
      never re-walks the preview subtree (a perf contract the pane keeps).
    * **Navigation** (``next``/``prev``) enumerates the match REGIONS fresh on
      the keypress — a snapshot goes stale as the lazy-mounted preview reflows
      (the reported bug: n did nothing because the snapshot was captured empty
      before a deep table laid out). Region reads here are fine: they happen on
      a deliberate keypress, never during a cold-nav settle.
    * **The ▲a/▼b view arrows** answer the original awareness question — "does
      the result I'm on have matches I can't see?" Users navigate *between*
      results with the results-pane arrows, which skip straight past matches
      lower down in the *same* chunk; the arrows count how many screenfuls
      ("views") of the CURRENT result hold an off-screen match, above and below.
      Everything here is scoped to the current chunk (``anchor.focus_chunk_seq``
      and its widget extent) — never the whole file. The counts ARE region-
      derived, but the read is deferred to settle-safe moments only (after the
      mount settles, on a nav keypress, on a user scroll) and cached, so the
      border refresh stays layout-free.

    ``n``/``b`` are likewise scoped: they hop between the current result's
    matching views, never crossing into another result (that stays the
    results-pane's job).
    """

    def __init__(self, app: FNDApp) -> None:
        self._app = app
        self._count = 0  # cached data-derived match count (footer-hint gate)
        # Cached matching-view counts for the current result, relative to the
        # viewport — the ▲a/▼b markers on the preview border. Region-derived
        # (unlike _count) but only re-measured at settle-safe moments (see
        # _measure_after_settle); the border reads the cache, never the regions.
        self._above = 0
        self._below = 0
        self._measure_pending = False
        # A re-measure asked for while one was already in flight. Dropping it
        # is what leaves the border showing a viewport the preview has left:
        # the in-flight poll can land on the PRE-scroll layout, so the request
        # it swallows is the one that would have caught the real position.
        self._measure_again = False
        # Confirmation passes left for this navigation. The counts are derived
        # from LAYOUT, but every trigger that refreshes them is a scroll or a
        # mount — so a late reflow (a table sizing its rows, a capture swapped
        # in) leaves the border describing a layout that is gone, with nothing
        # watching. Rather than enumerate the events that can move a stop, the
        # measurement re-confirms itself a bounded number of times.
        self._confirmations_left = 0
        self._last_target: int | None = None
        # Bumped per rebuild() so a superseded count tick self-cancels.
        self._refresh_gen = 0

    def _pane(self) -> VerticalScroll | None:
        from textual.containers import VerticalScroll

        try:
            return self._app.query_one("#preview_pane", VerticalScroll)
        except Exception:
            return None

    def _stops_within(self, root: Widget, spec: MatchSpec) -> int:
        """Match stops inside ``root``, from DATA only — no region reads, so no
        layout is forced. Mirrors ``enumerate_stop_regions``' stop set: one per
        matching table cell, one per non-table match block, one per matching
        plain line.

        Shared by the preview-wide count and the current-chunk check so the two
        can't drift on what counts as a stop — they answer the same question at
        different scopes, and a footer hint disagreeing with what n/b can reach
        is exactly the confusion this scan exists to prevent.
        """
        from textual.widgets import DataTable

        from fnd.render import text_has_any_match
        from fnd.tui.widgets.markdown import (
            FNDMarkdown,
            FNDMarkdownTableDT,
            FNDMarkdownTD,
            FNDMarkdownTH,
        )

        if spec.is_empty:
            return 0
        markdowns = [root] if isinstance(root, FNDMarkdown) else list(root.query(FNDMarkdown))
        n = 0
        for md in markdowns:
            for dt in md.query(DataTable):
                n += len(getattr(dt, "_fnd_match_coords", []))
            for block in md.match_blocks:
                if isinstance(block, FNDMarkdownTableDT | FNDMarkdownTD | FNDMarkdownTH):
                    continue
                n += 1
        # Frozen chunks carry their stop count from capture time. Counting them
        # keeps the footer hint and ``current_chunk_has_stops`` honest: without
        # it a frozen chunk reads as having no matches, so ``n``/``b`` would be
        # advertised as unavailable on a chunk that does have them — or, worse,
        # silently do nothing. Data only, no region reads, like everything else
        # in this method.
        from fnd.tui.preview.frozen import FrozenChunkView

        frozen = [root] if isinstance(root, FrozenChunkView) else list(root.query(FrozenChunkView))
        for view in frozen:
            n += len(view.frozen.stop_rows)
        for line in root.query("Static.chunk-line"):
            txt = getattr(line, "fnd_text", None)
            if txt and text_has_any_match(txt, spec):
                n += 1
        return n

    def _count_stops(self, pane: VerticalScroll) -> int:
        """Stops across the whole mounted preview — the footer-hint cache."""
        return self._stops_within(pane, self._app._effective_match_spec)

    def _region_stops(self, pane: VerticalScroll) -> list[int]:
        """Match stops as content-space tops (reads regions — call only on a
        deliberate navigation keypress, never during a cold-nav settle)."""
        from fnd.tui.preview_scroll import enumerate_stop_regions

        spec = self._app._effective_match_spec
        if spec.is_empty:
            return []
        base = pane.scrollable_content_region.offset.y
        oy = pane.scroll_offset.y
        return sorted(r.y - base + oy for r in enumerate_stop_regions(pane, spec))

    def _current_chunk_extent(self, pane: VerticalScroll) -> tuple[int, int] | None:
        """Content-space ``[top, bottom)`` of the CURRENT result's chunk — the
        one the results-pane selection revealed (``anchor.focus_chunk_seq``).
        ``bottom`` is the next mounted chunk's top (so it works whether the
        chunk widget is the full ``FNDMarkdown`` or just a plain chunk's first
        line); the last chunk extends to the content bottom. ``None`` when the
        current chunk can't be located (no anchor, or a flat preview with no
        per-chunk widgets) — callers then skip the arrows rather than guess."""
        ctrl = getattr(self._app, "_preview_scroll", None)
        anchor = getattr(ctrl, "anchor", None)
        preview = getattr(self._app, "_preview", None)
        widgets: dict[int, object] = getattr(preview, "chunk_widgets", None) or {}
        if anchor is None or not widgets:
            return None
        seq = anchor.focus_chunk_seq
        cur = widgets.get(seq)
        if cur is None:
            return None
        base = pane.scrollable_content_region.offset.y
        oy = pane.scroll_offset.y

        def ctop(w: object) -> int | None:
            region = getattr(w, "region", None)
            if region is None or region.height <= 0:
                return None
            return region.y - base + oy

        top = ctop(cur)
        if top is None:
            return None
        laters = [
            y for s, w in widgets.items() if s > seq and (y := ctop(w)) is not None and y > top
        ]
        bottom = min(laters) if laters else max(top + 1, pane.virtual_size.height)
        return top, bottom

    def _chunk_stops(self, pane: VerticalScroll) -> list[int]:
        """The current result's match stops (content-space tops). Falls back to
        every mounted stop when the chunk extent is unknown (flat preview) so
        ``n``/``b`` still work there, just unscoped."""
        stops = self._region_stops(pane)
        extent = self._current_chunk_extent(pane)
        if extent is None:
            return stops
        top, bottom = extent
        return [y for y in stops if top <= y < bottom]

    def _offscreen_views(self, pane: VerticalScroll) -> tuple[int, int]:
        """Matching views above / below the viewport, scoped to the current
        result. ``(0, 0)`` when the chunk extent is unknown (no false signal on
        flat previews). Reads regions — settle-safe callers only."""
        extent = self._current_chunk_extent(pane)
        if extent is None:
            return 0, 0
        lo, hi = extent
        stops = [y for y in self._region_stops(pane) if lo <= y < hi]
        top = pane.scroll_offset.y
        vh = pane.scrollable_content_region.height
        return offscreen_views(stops, top, top + vh, vh)

    @property
    def count(self) -> int:
        return self._count  # cached — cheap, no subtree walk

    def current_chunk_has_stops(self) -> bool:
        """Whether ``n``/``b`` can actually reach a match from where the user is.

        ``count`` spans the whole mounted preview, but ``_go`` operates on
        ``_chunk_stops`` — scoped to the current result's chunk. Gating the
        footer hint on ``count`` therefore advertised ``n/b Matches`` on a chunk
        where both keys silently no-op. Mirrors ``_chunk_stops``' own scoping
        rule, including its unscoped fallback, and reads data only (widget
        classes and registered match blocks), never regions — so it is safe on
        the same paths ``count`` is.
        """

        from fnd.tui.widgets.markdown import (
            FNDMarkdown,
        )

        if self._app._effective_match_spec.is_empty:
            return False
        ctrl = getattr(self._app, "_preview_scroll", None)
        anchor = getattr(ctrl, "anchor", None)
        widgets: dict[int, object] = getattr(self._app._preview, "chunk_widgets", None) or {}
        current = widgets.get(anchor.focus_chunk_seq) if anchor is not None else None
        if current is None:
            # Flat preview, or nothing mounted yet — _chunk_stops falls back to
            # every mounted stop here, so the file-wide count is the honest gate.
            return self._count > 0
        if isinstance(current, FNDMarkdown):
            return self._stops_within(current, self._app._effective_match_spec) > 0
        from fnd.tui.preview.frozen import FrozenChunkView

        if isinstance(current, FrozenChunkView):
            # A capture has no blocks and no match target — serving one pops the
            # target — so without this branch the plain-chunk fallback below
            # reads `None` and hides the `n/b Matches` hint on a chunk where both
            # keys work. Routed through `_stops_within` rather than reading
            # `stop_rows` here, so the count and this gate cannot drift on what
            # counts as a stop — which is the reason that method has a frozen arm.
            return self._stops_within(current, self._app._effective_match_spec) > 0
        # Plain per-line chunk: the mount records the first matching line as the
        # match target, falling back to the first line when nothing matched — so
        # the match class on that target is exactly "this chunk has a stop".
        target = self._app._preview.match_targets.get(anchor.focus_chunk_seq) if anchor else None
        return target is not None and target.has_class("chunk-line-match")

    @property
    def above(self) -> int:
        return self._above  # cached — matching views above the viewport (this result)

    @property
    def below(self) -> int:
        return self._below  # cached — matching views below the viewport (this result)

    @property
    def position(self) -> int | None:
        """1-based index of the stop last jumped to (clamped to the count), or
        ``None`` before any jump (or after a manual scroll)."""
        if self._last_target is None:
            return None
        return min(self._last_target + 1, self._count) if self._count else self._last_target + 1

    def on_manual_scroll(self) -> None:
        """Drop the burst memory so the next ``n``/``b`` is computed purely from
        the on-screen position, never resuming from the previous jump. A user
        scroll also moves matches across the fold, so re-measure the ▲/▼ markers
        (coalesced + settle-gated via :meth:`on_preview_scrolled`)."""
        self._last_target = None
        self.on_preview_scrolled()

    def rebuild(self) -> None:
        """Called when a preview mounts or the query changes: drop the burst
        memory and re-derive the cached count. Two phases so nothing touches the
        preview subtree during the cold-nav scroll-settle window."""
        self._refresh_gen += 1
        self._last_target = None
        # Drop stale arrows now and refresh the border this frame (real counts
        # land once the mount settles, via _measure_after_settle). Without the
        # notify the previous result's markers would linger until settle.
        #
        # Unconditional: this used to skip the notify when the arrow counts were
        # already (0, 0), which is exactly the case for a result the preview
        # can't paint. The border then kept whatever the PREVIOUS result had put
        # there — so the "match not shown here" notice never appeared on the
        # rows that need it, and never cleared on the rows that don't. One
        # border + footer update per preview mount is not worth the ambiguity.
        # ``_count`` too: it is the fallback ``current_chunk_has_stops`` uses
        # while no current chunk is resolvable, which is exactly the window a
        # mounting preview sits in — so a retained count advertised "n/b
        # Matches" for the result the user just navigated AWAY from. Recomputed
        # by the count tick below once the new preview is up.
        self._above = self._below = self._count = 0
        # Release the coalescing latch: the in-flight poll (if any) belongs to
        # the previous generation and will now exit without measuring, so
        # leaving the latch set would drop every request for THIS preview. The
        # deferred request it swallowed belongs to that generation too.
        self._measure_pending = False
        self._measure_again = False
        self._confirmations_left = _CONFIRMATIONS
        self._notify()
        self._await_mount(self._refresh_gen, retries=60)

    def _await_mount(self, gen: int, retries: int) -> None:
        """Phase 1: BARE poll for mount completion — no query, no region reads,
        nothing that touches the preview subtree — so the delicate cold-nav
        settle window is untouched (any per-frame subtree work there stalls the
        landing). Just wait for is_complete, then count.

        ``is_complete`` means every chunk of the file is mounted, which above
        ``FULLMOUNT_CHUNK_BUDGET`` is deliberately never true — the file stays
        windowed by design. So on a large file this runs its whole budget out and
        looks like pure waste. **It is not.** Burning the budget is what holds
        ``_count_tick``'s subtree walk back until after the mount, which is the
        guarantee the paragraph above is making. Ending the wait early puts that
        walk inside the settle window: measured reconcile-to-scroll 549 -> 912ms,
        worse in every round of an interleaved A/B.

        The protection is therefore accidental, and anything that makes
        ``is_complete`` reachable removes it silently. Gating on the landing
        instead (``is_settling`` plus ``pipeline_busy``) states it deliberately
        and measures neutral — correct, but not worth the churn on its own."""
        if gen != self._refresh_gen:
            return  # superseded by a newer rebuild
        active = getattr(getattr(self._app, "_preview", None), "active", None)
        if active is not None and not active.is_complete and retries > 0:
            self._app.call_after_refresh(lambda: self._await_mount(gen, retries - 1))
            return
        self._count_tick(gen, retries=3)

    def _count_tick(self, gen: int, retries: int) -> None:
        """Phase 2: the mount is complete — derive the DATA count (no region
        reads; match data is set at compose time, which is done by is_complete).
        Kept to a couple of passes (measured safe; many post-mount subtree walks
        are not) to catch a chunk that composes on the very next frame."""
        if gen != self._refresh_gen:
            return
        prev = self._count
        pane = self._pane()
        self._count = 0 if pane is None else self._count_stops(pane)
        self._notify()
        # Two guaranteed passes (catch a 0→N compose on the next frame), then
        # stop unless still changing.
        if retries > 0 and (retries > 1 or self._count != prev):
            self._app.call_after_refresh(lambda: self._count_tick(gen, retries - 1))
        else:
            # Count is stable; now measure the off-screen arrows — but only once
            # the nav scroll has provably landed (region reads mid-settle perturb
            # the cold-nav landing).
            self._measure_after_settle(gen, retries=30)

    def _poll_until_landed(
        self,
        retries: int,
        last_scroll: int | None,
        *,
        is_valid: Callable[[], bool],
        on_landed: Callable[[], None],
    ) -> None:
        """Reschedule until the reveal scroll has actually LANDED, then run
        ``on_landed``. Two conditions must hold: the controller is not settling
        (cold-nav gate) AND ``scroll_y`` has stopped moving — a warm nav commits
        its reveal scroll without ever flipping ``is_settling``, so the settle
        flag alone would measure against the pre-reveal viewport. Only the scroll
        reactive is read while polling (no layout), so the delicate settle path
        stays untouched; ``on_landed`` (the single region read) runs once landed.
        ``is_valid`` lets a superseded poll self-cancel each tick."""
        if not is_valid():
            return
        pane = self._pane()
        if pane is None:
            on_landed()  # pane gone (teardown) — let on_landed settle/clear state
            return
        cur = pane.scroll_offset.y
        ctrl = getattr(self._app, "_preview_scroll", None)
        settling = ctrl is not None and ctrl.is_settling
        moving = last_scroll is None or cur != last_scroll
        if retries > 0 and (settling or moving):
            self._app.call_after_refresh(
                lambda: self._poll_until_landed(
                    retries - 1, cur, is_valid=is_valid, on_landed=on_landed
                )
            )
            return
        on_landed()

    def _measure_after_settle(self, gen: int, retries: int) -> None:
        """Post-mount initial measure: land, then measure — self-cancelling if a
        newer rebuild superseded this generation."""
        self._poll_until_landed(
            retries,
            None,
            is_valid=lambda: gen == self._refresh_gen,
            on_landed=self._measure_offscreen,
        )

    def _arm_confirmation(self) -> None:
        """Re-measure once more after the layout has had time to stop moving."""
        import contextlib

        gen = self._refresh_gen
        # A stand-in app with no timer facility simply forgoes the re-confirm,
        # which is the pre-existing behaviour.
        with contextlib.suppress(Exception):
            self._app.set_timer(
                _CONFIRM_DELAY,
                lambda: self._schedule_measure() if gen == self._refresh_gen else None,
                name="match-nav-confirm",
            )

    def _measure_offscreen(self) -> None:
        """Re-derive the cached ▲/▼ view counts (current result, above/below the
        viewport) and refresh the border only when they changed. Reads regions —
        call only when the scroll is settled (post-settle, a deliberate nav, or a
        user scroll)."""
        pane = self._pane()
        above, below = (0, 0) if pane is None else self._offscreen_views(pane)
        if (above, below) != (self._above, self._below):
            self._above, self._below = above, below
            self._notify()

    def on_preview_scrolled(self) -> None:
        """The preview scrolled (user wheel/key, a reveal, or a warm-nav result
        switch). Re-measure the ▲/▼ markers once it lands. Coalesced + settle-
        gated, so a scroll burst collapses to one region read and nothing reads
        regions mid cold-nav settle."""
        self._schedule_measure()

    def on_result_revealed(self) -> None:
        """A new result was positioned in the preview — the AUTHORITATIVE switch
        event. The scroll watcher misses switches whose reveal doesn't move the
        scroll (so the old result's markers would linger); this fires regardless.
        Clear the old markers now, reset the burst memory (a new result is a
        fresh nav), and re-measure once the reveal position settles."""
        self._last_target = None
        self._confirmations_left = _CONFIRMATIONS
        if (self._above, self._below) != (0, 0):
            self._above = self._below = 0
            self._notify()
        self.on_preview_scrolled()

    def _schedule_measure(self) -> None:
        """Start a coalesced, settle-gated re-measure (idempotent while one is in
        flight). The poll reads only the scroll reactive until the scroll lands;
        the single region read happens at the end.

        Tied to the rebuild generation, like :meth:`_measure_after_settle`. An
        untied poll outlived the preview it was started for: a rebuild would
        clear the counts and advance the generation, and this poll would then
        land mid-mount, read regions during the cold-nav settle window, and
        repopulate ▲/▼ counts belonging to the previous result. Coalescing made
        it worse — while the stale poll was pending, requests for the NEW
        preview were dropped as duplicates.
        """
        if self._measure_pending:
            self._measure_again = True
            return
        self._measure_pending = True
        gen = self._refresh_gen

        def _landed() -> None:
            # Cleared BEFORE the generation check: returning with it still set
            # left the flag true forever, and every later request then returned
            # at the guard above — the markers stopped updating for the session.
            self._measure_pending = False
            if gen != self._refresh_gen:
                self._measure_again = False
                return  # superseded; the newer rebuild owns the measurement
            self._measure_offscreen()
            if self._measure_again:
                self._measure_again = False
                self._schedule_measure()
            elif self._confirmations_left > 0:
                self._confirmations_left -= 1
                self._arm_confirmation()

        self._poll_until_landed(
            30, None, is_valid=lambda: gen == self._refresh_gen, on_landed=_landed
        )

    def next(self) -> None:
        self._go(forward=True)

    def prev(self) -> None:
        self._go(forward=False)

    def _go(self, *, forward: bool) -> None:
        pane = self._pane()
        if pane is None:
            return
        # Scope to the CURRENT result's chunk so n/b reveal its hidden matches
        # and stop at its boundaries — never wandering into the next result
        # (that's the results-pane's job). Fresh each press — never a stale snap.
        stops = self._chunk_stops(pane)
        if not stops:
            return
        scroll_y = pane.scroll_offset.y
        vh = pane.scrollable_content_region.height
        if vh <= 0:
            return  # pane not laid out yet — nothing meaningful to hop within
        # Same margin the actual scroll lands with, so the burst reference
        # viewport (_ref_top) matches where a jump really put the last match.
        margin = _landing_margin(vh)
        chooser = next_stop_index if forward else prev_stop_index
        k = chooser(stops, scroll_y, vh, self._last_target, margin)
        self._last_target = k
        self._scroll_to_stop(pane, stops[k], vh)
        # Re-measure the view arrows AFTER the scroll commits — reading regions
        # synchronously here (before layout settles) yields an unresolved
        # viewport. Coalesced; runs on the next refresh with the scroll applied.
        self._schedule_measure()

    def _scroll_to_stop(self, pane: VerticalScroll, top_y: int, vh: int) -> None:
        from textual.geometry import Region

        # Drop the match ~a quarter down the viewport for context above it — the
        # same margin _go feeds the choosers (via _landing_margin).
        margin = _landing_margin(vh)
        region = Region(0, max(0, top_y - margin), 1, vh)
        # Flag this as a controller-owned scroll so the scroll watcher doesn't
        # treat it as a user scroll (which would clear the burst memory we just
        # set). Mirrors StructuralScrollStrategy's reconcile-scroll guard.
        # ``immediate`` (not animated) so the scroll commits synchronously
        # inside the reconcile window — an animated scroll's watcher trips fire
        # after ``end_reconcile_scroll``, outside the guard, and would then be
        # mistaken for a user scroll.
        preview = getattr(self._app, "_preview", None)
        if preview is not None:
            preview.begin_reconcile_scroll()
        try:
            pane.scroll_to_region(region, top=True, animate=False, immediate=True)
            self._app._diag_log(f"scroll site=nb_stop top_y={top_y}")
        finally:
            if preview is not None:
                preview.end_reconcile_scroll()

    def _notify(self) -> None:
        # Count → preview border; n/b key hint → footer keybinding area.
        with contextlib.suppress(Exception):
            self._app._refresh_preview_match_indicator()
        with contextlib.suppress(Exception):
            self._app._refresh_footer_hints()

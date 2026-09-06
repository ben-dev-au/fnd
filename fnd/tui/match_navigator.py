"""Intra-file preview match navigation (n / b).

The results-pane arrows step between results; within a result whose chunk is
taller than the viewport they skip straight past matches below the fold. This
module hops between those hidden matches by viewport and surfaces ``▲a ▼b``
view counts so the user knows they exist.

A hop stays inside the current result's chunk. At its edge the press HANDS OVER
to the file's adjacent listed section in document order, by moving the results
selection — so leaving a section is always a visible result switch, never a
silent scroll into a neighbour's match. The counts stay per-section, which is
what they are for: ``▼0`` means the next press leaves this section, not that it
does nothing.

The geometry (next/prev stop, hop counting) is pure and unit-tested; region
resolution + scrolling live on :class:`MatchNavigator` below.
"""

from __future__ import annotations

import contextlib
import time
from bisect import bisect_right
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.containers import VerticalScroll
    from textual.widget import Widget

    from fnd.matching import MatchSpec
    from fnd.tui.app import FNDApp


def view_anchors(ys: list[int], vh: int, home: int | None = None) -> list[int]:
    """Content-space tops of the views ``n``/``b`` step through — a greedy tiling
    of the stops, each view spanning ``vh`` from the first match it reaches.

    ONE list drives both directions and both border counts, so a hop and a count
    cannot disagree. Stops within one viewport of an anchor share its view, so
    the document's last screenful is one view however many matches fall in it and
    a pane clamped at ``max_scroll_y`` still shows them all.

    ``home`` is where the results pane landed, and anchors the first view when it
    already shows the first stop: a landing sits a quarter-viewport ABOVE the
    match, so tiling from the match leaves it a position no key can return to.
    """
    if vh <= 0:
        return []  # unlaid-out / hidden pane — no meaningful screenful width
    anchors: list[int] = []
    covered_to: int | None = None
    if home is not None and ys and home <= ys[0] < home + vh:
        anchors.append(home)
        covered_to = home + vh
    for y in ys:
        if covered_to is None or y >= covered_to:
            anchors.append(y)
            covered_to = y + vh
    return anchors


def adjacent_section(seqs: list[int], current: int, *, forward: bool) -> int | None:
    """The listed section ``n``/``b`` hands over to once ``current`` is exhausted
    — the next (or previous) in DOCUMENT order, wrapping at the file's ends, and
    ``None`` when there is nowhere to go. Not results order: the tree ranks by
    score, so its neighbouring row is somewhere else in the file entirely."""
    ordered = sorted(set(seqs))
    if not ordered or ordered == [current]:
        return None
    if forward:
        return next((s for s in ordered if s > current), ordered[0])
    return next((s for s in reversed(ordered) if s < current), ordered[-1])


def _view_of(anchors: list[int], y: int) -> int:
    """Index of the view that shows stop ``y`` — the last anchor at or above it.
    Every stop falls in a view by construction of :func:`view_anchors`."""
    return max(0, bisect_right(anchors, y) - 1)


def step_view(
    anchors: list[int], stops: list[int], top: int, vh: int, *, forward: bool
) -> int | None:
    """Where ``n`` (or ``b``) moves the viewport from ``top``, or ``None`` when
    nothing off screen lies that way.

    Keyed on the first STOP off screen, not on the current view's index: a
    results landing leaves the viewport part-way through its view, so the view
    holding it and the rows it shows are different spans.
    """
    if not anchors:
        return None
    if forward:
        nxt = next((y for y in stops if y >= top + vh), None)
        if nxt is not None:
            return anchors[_view_of(anchors, nxt)]
        wrap = anchors[0]
    else:
        prv = next((y for y in reversed(stops) if y < top), None)
        if prv is not None:
            return anchors[_view_of(anchors, prv)]
        wrap = anchors[-1]
    # Nothing off screen that way, so this is the wrap. Worth taking only if the
    # view it lands on shows a match this viewport does not.
    return wrap if _reveals(stops, wrap, top, vh) else None


def _reveals(stops: list[int], target: int, top: int, vh: int) -> bool:
    """Whether a viewport at ``target`` shows a stop one at ``top`` does not."""
    return any(target <= y < target + vh and not (top <= y < top + vh) for y in stops)


def offscreen_views(anchors: list[int], stops: list[int], top: int, vh: int) -> tuple[int, int]:
    """``(views above, views below)`` — the ``▲a ▼b`` border: views holding a
    match this viewport does not show.

    Counted over the same stops :func:`step_view` walks, so ``▼2`` is two more
    screenfuls holding a match AND two more presses of ``n``.
    """
    if not anchors:
        return 0, 0
    above = {_view_of(anchors, y) for y in stops if y < top}
    below = {_view_of(anchors, y) for y in stops if y >= top + vh}
    return len(above), len(below)


# Re-measure spacing, and the window a navigation keeps re-measuring in. The
# window is what stops it, not agreement between readings: two equal samples
# prove only that two samples matched, and this cache has already been wrong
# that way twice.
#
# The read is `enumerate_stop_regions` over the pane subtree, so it scales with
# the mounted STOPS, not just the chunks: 1.2ms warm at 13 chunks / 36 stops,
# 2.6ms at 102 chunks / 882 stops (the densest in this corpus) and 3.5ms at 1592
# stops on a synthetic shape denser than anything in it, against a
# `FULLMOUNT_CHUNK_BUDGET` of 250. At 4Hz that is ~2-3% of one core, and
# `on_result_revealed` re-opens the window, so walking a results list keeps it
# running rather than the 40 reads a single navigation costs. It stops 10s after
# the last one.
_CONFIRM_DELAY = 0.25
_CONFIRM_BUDGET = 10.0


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

    ``n``/``b`` hop between the current result's matching views, and at its edge
    hand over to the file's adjacent listed section (see the module docstring).
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
        # Deadline for the re-measures described at ``_CONFIRM_BUDGET``. The
        # counts derive from LAYOUT while every trigger that refreshes them is a
        # scroll or a mount, so a late reflow strands them with nothing watching.
        self._confirm_until = 0.0
        # The generation a confirmation chain is armed for, or None. Keyed by
        # generation, not a bare flag: a chain armed under an older one is dead
        # on arrival, and a flag cannot say so — it just blocks the arm the new
        # navigation needs, leaving an open window with nothing running.
        self._confirm_armed_gen: int | None = None
        # The last hop's destination and the landing the chunk was entered at,
        # both stored as offsets FROM THE CHUNK'S TOP and keyed by its seq. A
        # mount above the window shifts every stop (measured: +5 rows between two
        # presses), so an absolute content y goes stale mid-walk; and an absolute
        # y is ambiguous across chunks besides (a 2033 against a neighbour's
        # 2034).
        self._last_rel: int | None = None
        self._last_seq: int | None = None
        self._home_rel: int | None = None
        self._home_seq: int | None = None
        # Bumped per rebuild() so a superseded count tick self-cancels.
        self._refresh_gen = 0

    def _home(self, base: int) -> int | None:
        """The landing this chunk was entered at, in content space now, or
        ``None`` when it belongs to another chunk. ``base`` is the chunk's
        current top: the stored offset is measured from it, so a reflow that
        moves the chunk moves the landing with it."""
        if self._home_rel is None or self._home_seq != self._focus_seq():
            return None
        return self._home_rel + base

    def _last(self, base: int) -> int | None:
        """The last hop's destination in content space now, or ``None``."""
        if self._last_rel is None or self._last_seq != self._focus_seq():
            return None
        return self._last_rel + base

    def _focus_seq(self) -> int | None:
        """The chunk the results pane revealed, or ``None`` before any."""
        ctrl = getattr(self._app, "_preview_scroll", None)
        anchor = getattr(ctrl, "anchor", None)
        return None if anchor is None else anchor.focus_chunk_seq

    def _pane(self) -> VerticalScroll | None:
        from textual.containers import VerticalScroll

        try:
            return self._app.query_one("#preview_pane", VerticalScroll)
        except Exception:
            return None

    def _stops_within(self, root: Widget, spec: MatchSpec) -> int:
        """Match stops inside ``root``, from DATA only — no region reads, so no
        layout is forced: one per matching table cell, one per non-table match
        block, one per matching plain line.

        Deliberately per BLOCK where ``enumerate_stop_regions`` is per row: the
        rows need layout, and this runs on paths that must not force it. They go
        to zero together on a laid-out subtree only: a mounted block with no
        geometry counts here and yields no region, so the hint can lead the keys
        while a fill is hiding chunks.

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
        ``bottom`` is the next chunk's top (so it works whether the chunk widget
        is the full ``FNDMarkdown`` or just a plain chunk's first line); the last
        chunk extends to the content bottom. ``None`` ONLY for a preview with no
        per-chunk widgets at all (flat), where the caller's whole-preview
        fallback is right; an unresolvable chunk yields an EMPTY extent instead,
        because that fallback is otherwise a licence to leave the chunk."""
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
            return 0, 0  # EMPTY, not None: None unscopes the caller (see docstring)
        # Bound on the next chunk that HAS laid out: a zero-height one leaks no
        # stop (every arm of ``enumerate_stop_regions`` drops one), and for a
        # plain chunk ``chunk_widgets`` holds only its first LINE.
        laters = [
            y for s2, w in widgets.items() if s2 > seq and (y := ctop(w)) is not None and y > top
        ]
        bottom = min(laters) if laters else max(top + 1, pane.virtual_size.height)
        return top, bottom

    def _chunk_stops(self, pane: VerticalScroll) -> list[int]:
        """The current result's match stops (content-space tops). An unknown
        extent (flat preview) falls back to every mounted stop — which on that
        substrate is none, so the keys are inert rather than unscoped."""
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
        vh = pane.scrollable_content_region.height
        # Not pinned here: a landing may still be committing. _go pins it.
        home = self._home(lo)
        if home is None:
            home = pane.scroll_offset.y
        return offscreen_views(view_anchors(stops, vh, home), stops, pane.scroll_offset.y, vh)

    @property
    def count(self) -> int:
        return self._count  # cached — cheap, no subtree walk

    def current_chunk_has_stops(self) -> bool:
        """Whether ``n``/``b`` can do anything from where the user is: walk this
        section's own matches, or hand over to another listed section."""
        return self._chunk_has_match_data() or self.can_hop_section()

    def _is_dead_end(self) -> bool:
        """Whether this chunk offers nothing to walk to — the only state a
        hand-over may fire from when no stop resolved. A chunk still mounting or
        still building is not one: the data gate reads False in both windows, and
        handing over there walks the reader out of the section mid-navigation."""
        preview = getattr(self._app, "_preview", None)
        widgets: dict[int, object] = getattr(preview, "chunk_widgets", None) or {}
        seq = self._focus_seq()
        if seq is None or seq not in widgets:
            return False
        # Its blocks register DURING the build, so an empty match set before
        # ``build_done`` means "not yet". This is the wide window — seconds on a
        # big chunk — where the un-mounted one above is a tick.
        build_done = getattr(widgets[seq], "build_done", None)
        if build_done is not None and not build_done.is_set():
            return False
        return not self._chunk_has_match_data()

    def _chunk_has_match_data(self) -> bool:
        """Whether the current chunk carries a match at all.

        ``count`` spans the whole mounted preview, but ``_go`` operates on
        ``_chunk_stops`` — scoped to the current result's chunk. Gating the
        footer hint on ``count`` therefore advertised ``n/b Matches`` on a chunk
        where both keys silently no-op. Mirrors ``_chunk_stops``' own scoping,
        and reads data only — never regions — so it answers while the layout is
        still resolving, which is what tells a mid-mount press from a dead end.
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

    def on_manual_scroll(self) -> None:
        """Drop the burst memory so the next ``n``/``b`` is computed purely from
        the on-screen position, never resuming from the previous jump. A user
        scroll also moves matches across the fold, so re-measure the ▲/▼ markers
        (coalesced + settle-gated via :meth:`on_preview_scrolled`)."""
        self._last_rel = self._last_seq = None
        self._home_rel = self._home_seq = None
        self.on_preview_scrolled()

    def rebuild(self) -> None:
        """Called when a preview mounts or the query changes: drop the burst
        memory and re-derive the cached count. Two phases so nothing touches the
        preview subtree during the cold-nav scroll-settle window."""
        self._refresh_gen += 1
        self._last_rel = self._last_seq = None
        self._home_rel = self._home_seq = None
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
        self._confirm_armed_gen = None
        self._open_confirmation_window()
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

    def _open_confirmation_window(self) -> None:
        """A navigation starts the layout moving; re-measure until it stops."""
        self._confirm_until = time.monotonic() + _CONFIRM_BUDGET
        self._arm_confirmation()

    def _arm_confirmation(self) -> None:
        """Re-measure once more after the layout has had time to stop moving.

        One chain at a time. ``on_result_revealed`` opens a window without
        bumping the generation, so an arm per switch would leave a sweep of the
        results list running a chain per row, all of them reading regions.
        """
        gen = self._refresh_gen
        if self._confirm_armed_gen == gen:
            return
        try:
            self._app.set_timer(
                _CONFIRM_DELAY, lambda: self._confirm_tick(gen), name="match-nav-confirm"
            )
        except Exception:
            return  # a stand-in app with no timer facility forgoes the re-confirm
        self._confirm_armed_gen = gen

    def _confirm_tick(self, gen: int) -> None:
        """One confirmation: read now unless a navigation is mid-settle.

        Not routed through :meth:`_schedule_measure`, whose landing poll
        reschedules per refresh — a window of that is per-frame work on the
        settle path. A skipped tick costs nothing; the next is 250ms away."""
        if gen != self._refresh_gen:
            return  # superseded; its own generation's chain owns the window
        self._confirm_armed_gen = None
        # Textual hands a raising timer callback to ``App._handle_exception``,
        # which takes the app down. A region read on a preview mid-teardown is
        # exactly where that would come from, and the counts are decoration.
        with contextlib.suppress(Exception):
            ctrl = getattr(self._app, "_preview_scroll", None)
            if ctrl is None or not ctrl.is_settling:
                self._measure_offscreen()
                self._recount_if_empty()
        if time.monotonic() < self._confirm_until:
            self._arm_confirmation()

    def _recount_if_empty(self) -> None:
        """``_count_tick``'s ladder is three refreshes; a chunk that composes
        later leaves the count at 0 with nothing pending to fix it. Walk only
        while it reads zero, so the cost is paid in the broken state alone."""
        if self._count:
            return
        pane = self._pane()
        found = 0 if pane is None else self._count_stops(pane)
        if found:
            self._count = found
            self._notify()

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
        self._last_rel = self._last_seq = None
        self._home_rel = self._home_seq = None
        self._open_confirmation_window()
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
            # Cleared BEFORE the generation check, so a superseded poll cannot
            # leave the latch set. Defence in depth: `_poll_until_landed` gates
            # on the same generation in the frame it calls this.
            self._measure_pending = False
            if gen != self._refresh_gen:
                self._measure_again = False
                return  # superseded; the newer rebuild owns the measurement
            self._measure_offscreen()
            if self._measure_again:
                self._measure_again = False
                self._schedule_measure()
            elif time.monotonic() < self._confirm_until:
                self._arm_confirmation()

        self._poll_until_landed(
            30, None, is_valid=lambda: gen == self._refresh_gen, on_landed=_landed
        )

    def next(self) -> None:
        self._go(forward=True)

    def prev(self) -> None:
        self._go(forward=False)

    def _active_parent(self) -> str | None:
        """The file both the preview and the anchor name, or ``None`` when they
        disagree — a whole navigation long, since the anchor is armed before the
        container is activated. ``None`` on a flat preview, which nulls
        ``active`` and contributes no stops for n/b to walk."""
        preview = getattr(self._app, "_preview", None)
        parent = getattr(getattr(preview, "active", None), "parent_doc_id", None)
        anchor = getattr(getattr(self._app, "_preview_scroll", None), "anchor", None)
        armed = getattr(anchor, "parent_id", None)
        if parent is None or (armed is not None and armed != parent):
            return None
        return parent

    def _listed_sections(self) -> list[int]:
        """Chunk seqs of the current file's listed sections, in document order."""
        parent = self._active_parent()
        if parent is None:
            return []
        groups = getattr(getattr(self._app, "_search", None), "groups", None) or []
        for group in groups:
            if group.parent_id == parent:
                return sorted(h.chunk_seq for h in group.hits)
        return []

    def _select_section_row(self, seq: int) -> bool:
        """Put the results cursor on this file's row for ``seq``, which is what
        performs the hand-over: the highlight drives the normal result landing,
        so mounting and windowing stay on the path that already handles them."""
        from fnd.tui.widgets.results_tree import ResultsTree

        try:
            tree = self._app.query_one("#results_pane", ResultsTree)
        except Exception:
            return False
        parent = self._active_parent()
        file_node = next(
            (
                node
                for node in tree.root.children
                if node.data
                and node.data.get("kind") == "file"
                and node.data["group"].parent_id == parent
            ),
            None,
        )
        if file_node is None:
            return False
        # Found in the tree's own model, so a row that does not exist costs no
        # expansion of a node the reader had collapsed.
        row = next(
            (
                child
                for child in file_node.children
                if child.data
                and child.data.get("kind") == "section"
                and child.data["hit"].chunk_seq == seq
            ),
            None,
        )
        if row is None:
            return False
        if not file_node.is_expanded:
            file_node.expand()  # its rows are what `_tree_lines` is read for
        line = next((i for i, tl in enumerate(tree._tree_lines) if tl.node is row), None)
        if line is None:
            return False
        preview = getattr(self._app, "_preview", None)
        if preview is not None:
            # A scan (Option+arrow) suppresses the load the highlight triggers,
            # and only the tree's own key handler clears it.
            preview._scan_move = False
        if tree.cursor_line == line:
            # ``move_cursor_to_line`` early-returns on the line it is already on,
            # so no highlight fires and nothing loads. Reachable whenever a scan
            # has moved the cursor ahead of the preview.
            self._app._load_result_node(row.data)
        else:
            tree.move_cursor_to_line(line)
        return True

    def can_hop_section(self) -> bool:
        """Whether a hand-over is available from here — data only (results rows
        and the active file), so it is safe wherever the footer gate is.

        Never with highlights off: the spec is empty, there is nothing to walk
        to, and a hand-over would be the only thing the keys still did."""
        if self._app._effective_match_spec.is_empty:
            return False
        seq = self._focus_seq()
        if seq is None:
            return False
        return adjacent_section(self._listed_sections(), seq, forward=True) is not None

    def _hop_section(self, *, forward: bool) -> bool:
        """Hand over to the adjacent listed section of this file, in document
        order. Returns whether it was taken."""
        seq = self._focus_seq()
        if seq is None or self._app._effective_match_spec.is_empty:
            return False
        target = adjacent_section(self._listed_sections(), seq, forward=forward)
        if target is None:
            return False
        preview = getattr(self._app, "_preview", None)
        parent = self._active_parent()
        if preview is not None and parent is not None and not forward:
            # The LANDING's intent, not a scroll of our own after it: the armed
            # anchor re-applies its position as later chunks freeze, overwriting
            # anything scrolled behind its back.
            preview.pending_landing_intent = (parent, target, "last_match")
        if not self._select_section_row(target):
            if preview is not None:
                preview.pending_landing_intent = None
            return False
        return True

    def _go(self, *, forward: bool) -> None:
        pane = self._pane()
        if pane is None:
            return
        # Scope to the CURRENT result's chunk so a hop reveals its hidden matches
        # and stops at its boundaries; the edge is where the hand-over below takes
        # over. Fresh each press — never a stale snap.
        stops = self._chunk_stops(pane)
        vh = pane.scrollable_content_region.height
        if not stops or vh <= 0:
            # Only a dead end may hand over; a chunk still arriving must wait.
            if not self._is_dead_end() or not self._hop_section(forward=forward):
                self._schedule_measure()  # no scroll, so nothing else refreshes ▲▼
            return
        extent = self._current_chunk_extent(pane)
        base = extent[0] if extent is not None else 0
        home = self._home(base)
        if home is None:  # first press in this chunk: pin where the landing put us
            home = pane.scroll_offset.y
            self._home_rel, self._home_seq = home - base, self._focus_seq()
        anchors = view_anchors(stops, vh, home)
        top = pane.scroll_offset.y
        last = self._last(base)
        if last is not None and last in set(anchors):
            top = last
        # Nothing off screen this way: hand over rather than wrap inside the
        # section. A file with only this one keeps the wrap, in step_view.
        off_screen = any(y >= top + vh for y in stops) if forward else any(y < top for y in stops)
        if not off_screen and self._hop_section(forward=forward):
            return
        target = step_view(anchors, stops, top, vh, forward=forward)
        if target is None:
            self._schedule_measure()  # no scroll, so nothing else refreshes ▲▼
            return
        self._last_rel, self._last_seq = target - base, self._focus_seq()
        self._scroll_to_stop(pane, target, vh)
        # Re-measure the view arrows AFTER the scroll commits — reading regions
        # synchronously here (before layout settles) yields an unresolved
        # viewport. Coalesced; runs on the next refresh with the scroll applied.
        self._schedule_measure()

    def _scroll_to_stop(self, pane: VerticalScroll, top_y: int, vh: int) -> None:
        from textual.geometry import Region

        # The anchor IS the view's top: views tile by exactly one viewport, so a
        # hop covers a screenful and the border's count of screenfuls and of
        # presses are the same number. Offsetting the match down the viewport
        # here would shift every view off its tile and split them again.
        region = Region(0, max(0, top_y), 1, vh)
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

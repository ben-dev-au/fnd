"""Intra-file preview match navigation (n / b).

Steps between the match stops of the currently-mounted preview, hopping by
viewport so an off-screen match in the same file is reachable without manual
scrolling. The geometry (which stop is next/prev) is pure and unit-tested;
region resolution + scrolling live on :class:`MatchNavigator` below.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual.containers import VerticalScroll

    from fnd.tui.app import FNDApp


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
    # press up, mirroring one press down.
    window_top = ys[nearest_above] - (viewport_h - 2 * margin)
    target = nearest_above
    for i in range(nearest_above, -1, -1):
        if ys[i] >= window_top:
            target = i
        else:
            break
    return target


class MatchNavigator:
    """Owns intra-file match navigation for the live preview pane.

    Count and navigation deliberately use DIFFERENT data so each stays correct,
    cheap, AND free of side effects on unrelated scroll timing:

    * **The k/N footer count** is derived from match DATA — ``_fnd_match_coords``
      on the mounted tables + each chunk's ``match_blocks`` — which are plain
      list attributes set at compose time. Counting them never reads a region,
      so it never forces layout, so it can't perturb the delicate cold-nav
      scroll-settle window (region reads did — measured to stall the landing).
      The count is cached; the footer reads the cache, so a focus change never
      re-walks the preview subtree (a perf contract the pane keeps).
    * **Navigation** (``next``/``prev``) enumerates the match REGIONS fresh on
      the keypress — a snapshot goes stale as the lazy-mounted preview reflows
      (the reported bug: n did nothing because the snapshot was captured empty
      before a deep table laid out). Region reads here are fine: they happen on
      a deliberate keypress, never during a cold-nav settle.
    """

    def __init__(self, app: FNDApp) -> None:
        self._app = app
        self._count = 0  # cached data-derived match count (footer indicator)
        self._last_target: int | None = None
        self._margin = 4
        # Bumped per rebuild() so a superseded count tick self-cancels.
        self._refresh_gen = 0

    def _pane(self) -> VerticalScroll | None:
        from textual.containers import VerticalScroll

        try:
            return self._app.query_one("#preview_pane", VerticalScroll)
        except Exception:
            return None

    def _count_stops(self, pane: VerticalScroll) -> int:
        """Number of match stops from DATA only — no region reads, so no layout
        is forced. Mirrors ``enumerate_stop_regions``' stop set: one per matching
        table cell, one per non-table match block, one per matching plain line."""
        from textual.widgets import DataTable

        from fnd.render import text_has_any_match
        from fnd.tui.widgets.markdown import (
            FNDMarkdown,
            FNDMarkdownTableDT,
            FNDMarkdownTD,
            FNDMarkdownTH,
        )

        spec = self._app._effective_match_spec
        if spec.is_empty:
            return 0
        n = 0
        for md in pane.query(FNDMarkdown):
            for dt in md.query(DataTable):
                n += len(getattr(dt, "_fnd_match_coords", []))
            for block in md.match_blocks:
                if isinstance(block, FNDMarkdownTableDT | FNDMarkdownTD | FNDMarkdownTH):
                    continue
                n += 1
        for line in pane.query("Static.chunk-line"):
            txt = getattr(line, "fnd_text", None)
            if txt and text_has_any_match(txt, spec):
                n += 1
        return n

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

    @property
    def count(self) -> int:
        return self._count  # cached — cheap, no subtree walk

    @property
    def position(self) -> int | None:
        """1-based index of the stop last jumped to (clamped to the count), or
        ``None`` before any jump (or after a manual scroll)."""
        if self._last_target is None:
            return None
        return min(self._last_target + 1, self._count) if self._count else self._last_target + 1

    def on_manual_scroll(self) -> None:
        """Drop the burst memory so the next ``n``/``b`` is computed purely from
        the on-screen position, never resuming from the previous jump."""
        self._last_target = None

    def rebuild(self) -> None:
        """Called when a preview mounts or the query changes: drop the burst
        memory and re-derive the cached count. Two phases so nothing touches the
        preview subtree during the cold-nav scroll-settle window."""
        self._refresh_gen += 1
        self._last_target = None
        self._await_mount(self._refresh_gen, retries=60)

    def _await_mount(self, gen: int, retries: int) -> None:
        """Phase 1: BARE poll for mount completion — no query, no region reads,
        nothing that touches the preview subtree — so the delicate cold-nav
        settle window is untouched (any per-frame subtree work there stalls the
        landing). Just wait for is_complete, then count."""
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

    def next(self) -> None:
        self._go(forward=True)

    def prev(self) -> None:
        self._go(forward=False)

    def _go(self, *, forward: bool) -> None:
        pane = self._pane()
        if pane is None:
            return
        stops = self._region_stops(pane)  # fresh — never a stale snapshot
        if not stops:
            return
        scroll_y = pane.scroll_offset.y
        vh = pane.scrollable_content_region.height
        chooser = next_stop_index if forward else prev_stop_index
        k = chooser(stops, scroll_y, vh, self._last_target, self._margin)
        self._last_target = k
        self._count = len(stops)  # keep the indicator in sync with what nav sees
        self._scroll_to_stop(pane, stops[k], vh)
        self._notify()

    def _scroll_to_stop(self, pane: VerticalScroll, top_y: int, vh: int) -> None:
        from textual.geometry import Region

        # Drop the match ~a quarter down the viewport for context above it.
        margin = int(vh * 0.25)
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
        finally:
            if preview is not None:
                preview.end_reconcile_scroll()

    def _notify(self) -> None:
        with contextlib.suppress(Exception):
            self._app._refresh_footer_hints()

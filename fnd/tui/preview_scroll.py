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
    from fnd.tui.app import FNDMarkdown


@dataclass(frozen=True, slots=True)
class ScrollAnchor:
    parent_id: str
    focus_chunk_seq: int
    intent: str = "first_match"  # or "chunk_top"
    context_fraction: float = 0.25


class ScrollStrategy(Protocol):
    def reconcile(self, anchor: ScrollAnchor) -> None: ...


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

    @property
    def is_armed(self) -> bool:
        return self._armed

    @property
    def anchor(self) -> ScrollAnchor | None:
        return self._anchor

    def arm(self, anchor: ScrollAnchor) -> None:
        self._anchor = anchor
        self._armed = True

    def release(self) -> None:
        self._armed = False

    def reconcile(self) -> None:
        if not self._armed or self._anchor is None:
            return
        strategy = self._select_strategy()
        if strategy is not None:
            strategy.reconcile(self._anchor)


class StructuralHost(Protocol):
    """The slice of FNDApp the structural scroll strategy reads."""

    def preview_pane(self) -> VerticalScroll: ...
    def effective_match_spec(self) -> MatchSpec: ...
    def suppress_lazy_mount_briefly(self, duration: float = 0.4) -> None: ...
    def call_after_refresh(
        self, callback: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> object: ...
    def diag_log(self, msg: str) -> None: ...

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

    def reconcile(self, anchor: ScrollAnchor) -> None:
        self._do_scroll_to_chunk(anchor.focus_chunk_seq, margin_from=anchor.context_fraction)

    def _do_scroll_to_chunk(
        self,
        focus_chunk_seq: int,
        retries: int = 30,
        on_done: Callable[[], None] | None = None,
        margin_from: float = 0.25,
    ) -> None:
        from fnd.tui.app import FNDMarkdown

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
        first_match_seen = False
        chunk_md = target if hasattr(target, "first_match_block") else None
        if chunk_md is not None:
            inner = chunk_md.first_match_block  # pyright: ignore[reportAttributeAccessIssue]
            if inner is None and retries > 0:
                self._host.call_after_refresh(
                    self._do_scroll_to_chunk, focus_chunk_seq, retries - 1, on_done, margin_from
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
        if target.region.height == 0 and retries > 0:
            self._host.call_after_refresh(
                self._do_scroll_to_chunk, focus_chunk_seq, retries - 1, on_done, margin_from
            )
            return
        if target.region.height == 0:
            self._host.diag_log(
                f"do_scroll seq={focus_chunk_seq} miss=zero-region "
                f"target={type(target).__name__} path={path}"
            )
        pane = self._host.preview_pane()
        # Brief gate so the resulting watcher trip doesn't fire a lazy
        # mount that competes with this scroll's anchor.
        self._host.suppress_lazy_mount_briefly()
        # Drop the match ~a quarter down the viewport so the lines above it
        # give context, instead of pinning it to the top line — but only when
        # we actually landed on a match (not a bare chunk-top navigation).
        margin = int(pane.size.height * margin_from) if (first_match_seen or fallback_fired) else 0
        # A match inside a table renders as a single full-height DataTable
        # (one Rich render, no per-cell widgets and no internal scroll), so
        # scroll_to_widget would only reach the table's top. Scroll the pane
        # to the matched cell's region instead so a match in a lower row is
        # actually revealed.
        if not self._scroll_pane_to_table_cell(pane, target, margin):
            # Map the target widget's screen region into the pane's scrollable
            # content space and scroll there in one shot. (Reading scroll_offset
            # back after scroll_to_widget to apply the margin races a cold
            # render — scroll_to_widget hasn't committed the offset yet, so the
            # nudge lands on a stale, wrong position.)
            region = target.region.translate(
                pane.scroll_offset - pane.scrollable_content_region.offset
            )
            self._scroll_pane_to_match_region(pane, region, margin)
        self._host.diag_log(
            f"do_scroll seq={focus_chunk_seq} target={type(target).__name__} "
            f"path={path} first_match={first_match_seen} fallback={fallback_fired} "
            f"retries_used={30 - retries}"
        )
        if on_done is not None:
            on_done()

    def _scroll_pane_to_match_region(
        self, pane: VerticalScroll, region: Region, margin: int
    ) -> None:
        """Scroll ``pane`` so ``region`` (already in the pane's scrollable-
        content space) sits ``margin`` rows down from the top, giving the match
        some context above it. One ``scroll_to_region`` call — no reading the
        offset back, so nothing races a cold render's deferred layout."""
        if margin:
            region = Region(
                region.x, max(0, region.y - margin), region.width, region.height + margin
            )
        pane.scroll_to_region(region, top=True, animate=False, immediate=True)

    def _scroll_pane_to_table_cell(self, pane: VerticalScroll, target: Widget, margin: int) -> bool:
        """If ``target`` is (or wraps) a match-bearing DataTable, scroll
        ``pane`` to the matched cell and return True. The W3 table renders
        every row in one full-height DataTable with no internal scroll, so the
        matched cell is not a scrollable widget — translate its region into the
        pane's content space and scroll there. ``target`` may be the DataTable
        itself or the ``FNDMarkdownTableDT`` wrapper (the match scroll resolves
        to the wrapper when the first_match_block is a phantom, never-mounted
        TD cell). Returns False — the caller then scrolls to the target
        widget's own region via ``_scroll_pane_to_match_region`` — for
        non-table targets or any lookup failure."""
        from textual.widgets import DataTable

        from fnd.tui.app import FNDMarkdownTableDT

        if isinstance(target, DataTable):
            table = target
        elif isinstance(target, FNDMarkdownTableDT):
            table = next((c for c in target.query(DataTable)), None)
        else:
            return False
        if table is None:
            return False
        coord = getattr(table, "_fnd_match_coord", None)
        if coord is None:
            return False
        try:
            cell = table._get_cell_region(coord)  # pyright: ignore[reportAttributeAccessIssue]
            # cell is relative to the table's content; map → screen (the table
            # has no internal scroll, but honour its offset defensively) → the
            # pane's scrollable-content space, which scroll_to_region expects.
            screen = cell.translate(table.region.offset - table.scroll_offset)
            cell_in_pane = screen.translate(
                pane.scroll_offset - pane.scrollable_content_region.offset
            )
        except Exception:
            return False
        self._scroll_pane_to_match_region(pane, cell_in_pane, margin)
        return True

    def _fallback_match_target(self, chunk: FNDMarkdown) -> Widget:
        """Scan ``chunk``'s descendants for the first widget whose plain text
        contains a match. Used when no highlight-aware subclass claimed
        ``first_match_block`` (e.g. matches inside a MarkdownFence)."""
        spec = self._host.effective_match_spec()
        if spec.is_empty:
            return chunk
        from fnd.render import text_has_any_match

        for w in chunk.query("*"):
            if w is chunk:
                continue
            try:
                plain = w._content.plain  # type: ignore[attr-defined]
            except Exception:
                plain = None
            if plain is None:
                # MarkdownFence renders rich.syntax.Syntax — its text lives
                # on .code attribute set by build_from_token.
                plain = getattr(w, "code", None)
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
        from fnd.tui.app import FNDMarkdownTableDT

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

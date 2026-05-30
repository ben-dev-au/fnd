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
    from fnd.tui.line_buffer import LineBufferPreview


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
        self, anchor: ScrollAnchor, on_settled: Callable[[], None] | None = None
    ) -> None: ...
    def locate(self) -> ViewportLocation | None: ...
    def scroll_to_location(self, location: ViewportLocation) -> None: ...


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
        fire = _Once(on_settled)
        if not self._armed or self._anchor is None:
            fire()
            return
        strategy = self._select_strategy()
        if strategy is None:
            fire()
            return
        try:
            strategy.reconcile(self._anchor, fire)
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
        propagate into a UI event handler."""
        if location is None:
            return
        strategy = self._select_strategy()
        if strategy is None:
            return
        with contextlib.suppress(Exception):
            strategy.scroll_to_location(location)


class StructuralHost(Protocol):
    """The slice of FNDApp the structural scroll strategy reads."""

    def preview_pane(self) -> VerticalScroll: ...
    def effective_match_spec(self) -> MatchSpec: ...
    def begin_reconcile_scroll(self) -> None: ...
    def end_reconcile_scroll(self) -> None: ...
    def swap_reveal_target(self, target: Widget, margin: int) -> bool: ...
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

    def reconcile(self, anchor: ScrollAnchor, on_settled: Callable[[], None] | None = None) -> None:
        from fnd.tui.app import FNDMarkdown

        seq = anchor.focus_chunk_seq
        header = self._host.chunk_widgets.get(seq)
        if header is None:
            if on_settled is not None:
                on_settled()
            return
        # Move the focused-section accent band to the target chunk (FNDMarkdown
        # manages its own focus highlight internally, so skip the band there).
        for w in self._host.chunk_widgets.values():
            w.remove_class("chunk-section-focused")
        if not isinstance(header, FNDMarkdown):
            header.add_class("chunk-section-focused")
        self._host.call_after_refresh(
            self._do_scroll_to_chunk, seq, 30, on_settled, anchor.context_fraction, anchor.animate
        )

    def _do_scroll_to_chunk(
        self,
        focus_chunk_seq: int,
        retries: int = 30,
        on_done: Callable[[], None] | None = None,
        margin_from: float = 0.25,
        animate: bool = False,
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
                    self._do_scroll_to_chunk,
                    focus_chunk_seq,
                    retries - 1,
                    on_done,
                    margin_from,
                    animate,
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
                self._do_scroll_to_chunk,
                focus_chunk_seq,
                retries - 1,
                on_done,
                margin_from,
                animate,
            )
            return
        if target.region.height == 0:
            self._host.diag_log(
                f"do_scroll seq={focus_chunk_seq} miss=zero-region "
                f"target={type(target).__name__} path={path}"
            )
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
            self._host.begin_reconcile_scroll()
            try:
                # If an outgoing preview is being held on screen, hand the resolved
                # target to the host so it can hide the old one, position this one,
                # and reveal it in a single tick (no blank between previews). When
                # there is no outgoing container this is a no-op and we scroll the
                # already-visible pane normally.
                if self._host.swap_reveal_target(target, margin):
                    pass
                # A match inside a table renders as a single full-height DataTable
                # (one Rich render, no per-cell widgets and no internal scroll), so
                # scroll_to_widget would only reach the table's top. Scroll the pane
                # to the matched cell's region instead so a match in a lower row is
                # actually revealed.
                elif not self._scroll_pane_to_table_cell(pane, target, margin, animate=animate):
                    # Map the target widget's screen region into the pane's
                    # scrollable content space and scroll there in one shot.
                    # (Reading scroll_offset back after scroll_to_widget to apply
                    # the margin races a cold render — scroll_to_widget hasn't
                    # committed the offset yet, so the nudge lands on a stale,
                    # wrong position.)
                    region = target.region.translate(
                        pane.scroll_offset - pane.scrollable_content_region.offset
                    )
                    self._scroll_pane_to_match_region(pane, region, margin, animate=animate)
            finally:
                self._host.end_reconcile_scroll()
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

    def _scroll_pane_to_table_cell(
        self, pane: VerticalScroll, target: Widget, margin: int, *, animate: bool = False
    ) -> bool:
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
        self._scroll_pane_to_match_region(pane, cell_in_pane, margin, animate=animate)
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

    def scroll_to_location(self, location: ViewportLocation) -> None:
        if location.kind != "structural":
            return
        self._restore_structural(location.chunk_seq, location.offset, retries=12, last_vy=None)

    def _restore_structural(self, seq: int, delta: int, retries: int, last_vy: int | None) -> None:
        # A width reflow re-wraps the chunks above over several refreshes,
        # which keeps sliding the target chunk's content position. Track that
        # content position (``virtual_region.y`` — scroll-independent) and
        # re-apply until it stops moving; watching ``scroll_y`` instead settles
        # early (the offset repeats while the layout is still flowing) and
        # lands a chunk off. Scroll numerically to the chunk's content top plus
        # the captured in-chunk offset.
        w = self._host.chunk_widgets.get(seq)
        if w is None:
            return
        pane = self._host.preview_pane()
        vy = w.virtual_region.y
        # Flag as the controller's own scroll so the watcher trip isn't read
        # as a user scroll (which would self-release the anchor).
        self._host.begin_reconcile_scroll()
        try:
            pane.scroll_to(y=max(0, vy + delta), animate=False, immediate=True)
        finally:
            self._host.end_reconcile_scroll()
        # The width reflow re-wraps the chunks above asynchronously over many
        # refreshes, so the chunk's content position keeps moving after we
        # first scroll. Re-apply on every refresh for the whole budget (do NOT
        # early-stop: the position is stale-stable for a stretch, then jumps
        # once the re-wrap lands), re-reading it each time so the final applies
        # land on the settled layout.
        if retries > 0:
            self._host.call_after_refresh(self._restore_structural, seq, delta, retries - 1, vy)


class FlatHost(Protocol):
    """The slice of FNDApp the flat scroll strategy needs."""

    def active_flat_buffer(self) -> LineBufferPreview | None: ...


class FlatScrollStrategy:
    """Scroll the flat (PDF/TXT) line-buffer preview to a match.

    The ``LineBufferPreview`` owns the visual line math; this strategy only
    hands it the target chunk and the context margin. The dispatch re-arms the
    anchor with the resolved focus chunk, so the buffer's own first-match /
    chunk-top fallback handles the rest.
    """

    def __init__(self, host: FlatHost) -> None:
        self._host = host

    def reconcile(self, anchor: ScrollAnchor, on_settled: Callable[[], None] | None = None) -> None:
        buf = self._host.active_flat_buffer()
        if buf is None:
            if on_settled is not None:
                on_settled()
            return
        buf.scroll_to_chunk(
            anchor.focus_chunk_seq,
            prefer_first_match=True,
            context_fraction=anchor.context_fraction,
        )
        # The flat buffer scrolls synchronously, so the view has already
        # landed — reveal immediately.
        if on_settled is not None:
            on_settled()

    def locate(self) -> ViewportLocation | None:
        """The logical line at the viewport top — exact across a width reflow
        (the line buffer re-wraps but logical lines stay addressable)."""
        buf = self._host.active_flat_buffer()
        if buf is None:
            return None
        line = buf.top_logical_line()
        return None if line is None else ViewportLocation("flat", line=line)

    def scroll_to_location(self, location: ViewportLocation) -> None:
        if location.kind != "flat":
            return
        buf = self._host.active_flat_buffer()
        if buf is None:
            return
        # Exact (no context margin) — restore the *reading* position, not a
        # match drop. scroll_to_line re-wraps for the new width first.
        buf.scroll_to_line(location.line, context_fraction=0.0)

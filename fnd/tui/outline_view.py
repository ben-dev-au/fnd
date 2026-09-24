"""The Outline pane: what it shows, where its cursor sits, and its jumps.

The tree mirrors the document the preview is *showing*, never one still being
built, so mid-navigation it keeps the previous file until the swap lands. The
outline itself is built off the event loop (a long texturised PDF parses for a
noticeable fraction of a second) and replaces the tree only when it is ready.

Its cursor follows the reader. A navigation (every ``arm`` makes a new anchor)
moves it to the heading holding the target; after that, the reader is at the
reading line, the row a quarter down the preview where navigation lands
things. The one exception is a target the end of the document keeps off that
line: while the view still rests against that end and the target is on screen,
the reader is at the target. Sampling waits for the landing to settle and acts only when the
reader's heading changes. While the pane has focus the cursor is the user's:
the reader is still tracked, and the cursor catches up on leaving.
"""

from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING, Any

from rich.markup import escape
from textual.css.query import NoMatches

from fnd.tui.outline_model import Outline, OutlineEntry, build_outline
from fnd.tui.preview import tuning
from fnd.tui.preview.presenter import decode_abandonable
from fnd.tui.widgets.outline_tree import OutlineTree

if TYPE_CHECKING:
    from fnd.tui.app import FNDApp
    from fnd.tui.preview_scroll import ScrollAnchor

__all__ = ["OutlineView"]

NOTHING_PREVIEWED = "Nothing in the preview"
LOADING = "Loading outline…"
UNAVAILABLE = "Outline unavailable"
PANE_ID = "outline_pane"
# How far short of the document's end a view may sit and still rest against it:
# a Reading View round trip restores one row short.
EDGE_SLACK = 2


class OutlineView:
    """Drives ``#outline_pane`` from the preview's state; owns no widgets."""

    def __init__(self, app: FNDApp) -> None:
        self._app = app
        # The file the tree outlines and the chunk list it was built from.
        self._parent_id: str | None = None
        self._chunks: object | None = None
        # The chunk list an outline is being built from, if any.
        self._building: object | None = None
        self._shown = ""
        self._anchor: ScrollAnchor | None = None
        # The view (offset, height, scroll range) the reader was last sampled in.
        self._sampled: tuple[int, int, int] | None = None
        # The entry holding the reader, before any collapsed heading hides it,
        # and whether the cursor still owes a move to it.
        self._reader: int | None = None
        self._owed = False
        self._busy_at = 0.0

    def _tree(self) -> OutlineTree | None:
        try:
            return self._app.query_one(f"#{PANE_ID}", OutlineTree)
        except NoMatches:
            return None

    def title(self) -> str:
        tree = self._tree()
        count = len(tree.outline.entries) if tree is not None else 0
        marker = self._app._scope.collapsed_marker(PANE_ID)
        noun = "heading" if count == 1 else "headings"
        return escape(marker + (f"Outline: {count} {noun}" if count else "Outline"))

    def _refresh_title(self) -> None:
        tree = self._tree()
        if tree is not None:
            tree.border_title = self.title()

    # ── what the tree shows ──────────────────────────────────────────────

    def sync(self) -> None:
        """Rebuild the tree when the previewed document changed; cheap otherwise."""
        tree = self._tree()
        if tree is None:
            return
        preview = self._app._preview
        parent_id = preview.parent_id
        if parent_id is None:
            # A re-render (`h`) clears the preview and rebuilds it; keep the tree.
            if not preview.pipeline_busy():
                self._show(tree, NOTHING_PREVIEWED)
            return
        if not (preview.is_painted() and preview.showing_parent() == parent_id):
            return
        if "collapsed" in tree.classes or self._app._reading_mode:
            # Out of sight: no build. A file change drops the stale tree, so
            # reopening shows it loading rather than another file's headings.
            if parent_id != self._parent_id:
                self._show(tree, LOADING)
            return
        chunks = preview.chunk_cache.get(parent_id)
        if chunks is None:
            if parent_id != self._parent_id:
                self._show(tree, LOADING)
            return
        if (parent_id == self._parent_id and chunks is self._chunks) or chunks is self._building:
            return
        self._building = chunks
        self._app.run_worker(
            self._build(parent_id, chunks),
            group="outline-build",
            exclusive=True,
            exit_on_error=False,
        )

    async def _build(self, parent_id: str, chunks: Any) -> None:
        # A daemon thread, so a quit never waits on a build it would discard.
        try:
            outline = await decode_abandonable(build_outline, chunks)
        except Exception:
            outline = Outline(reason=UNAVAILABLE)
        self._apply(parent_id, chunks, outline)

    def _apply(self, parent_id: str, chunks: Any, outline: Outline) -> None:
        if chunks is not self._building:
            return  # a newer build owns the tree
        self._building = None
        tree = self._tree()
        if tree is None or self._app._preview.parent_id != parent_id:
            return  # the preview moved on while this was building
        unchanged = parent_id == self._parent_id and outline == tree.outline and not self._shown
        self._parent_id, self._chunks, self._shown = parent_id, chunks, ""
        if unchanged:
            return
        tree.set_outline(outline)
        self._refresh_title()
        self._forget_reader()
        if not self._landed():
            self._place_target(tree)  # the navigation that brought it here is still landing
        self.follow()
        if self._owed:  # a fresh tree has no browsing of the user's to keep
            self._owed = False
            self._place(tree, self._reader)

    def _show(self, tree: OutlineTree, placeholder: str) -> None:
        if self._shown == placeholder:
            return
        self._building = None
        self._parent_id = self._chunks = None
        self._shown = placeholder
        self._forget_reader()
        tree.set_outline(Outline(), placeholder)
        self._refresh_title()

    # ── where its cursor sits ────────────────────────────────────────────

    def tick(self) -> None:
        """Periodic reconcile: the document, then the cursor."""
        self.sync()
        self.follow()

    def follow(self) -> None:
        """Move the cursor to the reader: a new navigation's target, then the
        heading holding the reader as they scroll."""
        anchor = self._app._preview_scroll.anchor
        tree = self._tree()
        if anchor is not self._anchor:
            self._anchor = anchor
            self._sampled = None
            self._owed = False
            # A navigation can start and go idle between two polls; its settle
            # grace runs from when it was first seen, not the last busy poll.
            self._busy_at = time.monotonic()
            if tree is not None:
                self._place_target(tree)
            return
        if not self._landed():
            return
        showing = self._app._preview.showing_parent()
        if tree is not None and self._parent_id is not None and showing == self._parent_id:
            self._follow_reading(tree)

    def _place_target(self, tree: OutlineTree) -> None:
        anchor = self._anchor
        if anchor is None or anchor.parent_id != self._parent_id or not tree.outline.entries:
            return
        jumped = anchor.intent == "chunk_top" and anchor.heading >= 0
        passed = anchor.heading + 1 if jumped else None
        self._place(tree, tree.outline.index_for(anchor.focus_chunk_seq, headings_passed=passed))

    def _follow_reading(self, tree: OutlineTree) -> None:
        outline = tree.outline
        view = self._view()
        if outline.entries and view is not None and view != self._sampled:
            position = self._reader_position()
            if position is not None:
                self._sampled = view
                passed = self._headings_passed(outline, *position)
                index = outline.index_for(position[0], headings_passed=passed)
                if index != self._reader:
                    self._reader, self._owed = index, True
        if self._owed and not tree.has_focus:
            self._owed = False
            self._place(tree, self._reader)

    def _reader_position(self) -> tuple[int, int | None] | None:
        scroll = self._app._preview_scroll
        target = scroll.landing_target()
        if target is not None and self._held_off_the_line(target):
            return target
        return scroll.reading_position(tuning.MATCH_CONTEXT_FRACTION)

    def _held_off_the_line(self, target: tuple[int, int | None]) -> bool:
        """The landing's target is on screen, off the reading line, and the view
        still rests against the end of the document that stopped it there."""
        scroller = self._scroller()
        row = self._app._preview_scroll.view_row(*target)
        if scroller is None or row is None:
            return False
        height = scroller.size.height
        if not 0 <= row < height:
            return False
        gap = row - int(height * tuning.MATCH_CONTEXT_FRACTION)
        y = scroller.scroll_offset.y
        if gap > 0:  # below the line: the bottom of the document holds it there
            room = scroller.max_scroll_y - y
            return room < gap and room <= EDGE_SLACK
        return gap < 0 and y < -gap and y <= EDGE_SLACK

    def _forget_reader(self) -> None:
        self._sampled = self._reader = None
        self._owed = False

    def _landed(self) -> bool:
        """The last navigation's scroll has committed and nothing is gliding.
        ``is_settling`` can outlive a navigation that never reconciles, so it
        counts only within the reveal watchdog's bound of the pipeline idling."""
        preview = self._app._preview
        if preview.outgoing is not None or self._app._reading_mode:
            return False
        now = time.monotonic()
        if preview.pipeline_busy():
            self._busy_at = now
            return False
        settling = self._app._preview_scroll.is_settling
        if settling and (now - self._busy_at) * 1000.0 < tuning.REVEAL_WATCHDOG_MS:
            return False
        scroller = self._scroller()
        return scroller is not None and not self._app.animator.is_being_animated(
            scroller, "scroll_y"
        )

    def _scroller(self) -> Any:
        buffer = self._app._flat.active_buffer
        if buffer is not None:
            return buffer
        with contextlib.suppress(NoMatches):
            return self._app.query_one("#preview_pane")
        return None

    def _view(self) -> tuple[int, int, int] | None:
        scroller = self._scroller()
        if scroller is None:
            return None
        return int(scroller.scroll_offset.y), scroller.size.height, scroller.max_scroll_y

    def _headings_passed(self, outline: Outline, chunk_seq: int, row: int | None) -> int | None:
        """How many of the chunk's rendered headings sit at or above ``row``;
        asked only of a chunk holding an entry that does not open it."""
        if row is None or all(e.at_top for e in outline.entries if e.chunk_seq == chunk_seq):
            return None
        rows = self._app._preview_scroll.chunk_heading_rows(chunk_seq)
        return None if rows is None else sum(1 for r in rows if r <= row)

    def _place(self, tree: OutlineTree, index: int | None) -> None:
        self._reader = index
        current = tree.cursor_index()
        entries = tree.outline.entries
        if (
            index is not None
            and current is not None
            and _same_place(entries[current], entries[index])
        ):
            return
        tree.place_cursor(index)

    # ── jumps ────────────────────────────────────────────────────────────

    def jump(self, index: int | None) -> bool:
        """Send the preview to entry ``index``. False (and a notice) when the
        preview is not showing the outlined document settled."""
        tree = self._tree()
        if tree is None or index is None or not 0 <= index < len(tree.outline.entries):
            return False
        refusal = self._refusal()
        if refusal:
            self._app.notify(refusal, timeout=3)
            return False
        preview = self._app._preview
        parent_id = self._parent_id
        if parent_id is None:
            return False
        entry = tree.outline.entries[index]
        self._reader = index
        preview.jump_to_chunk_top(parent_id, entry.chunk_seq, entry.ordinal)
        return True

    def _refusal(self) -> str:
        """Why a jump cannot run now, or "" when it can."""
        preview = self._app._preview
        showing = preview.showing_parent()
        if self._parent_id is None or preview.outgoing is not None or not preview.is_painted():
            return "The preview is still loading."
        if showing != self._parent_id:
            return "The outline is still catching up with the preview."
        target = preview.cursor_target()
        if target is None or target[0] != self._parent_id:
            # The paint check would load the Results cursor's file straight back.
            return "The Results cursor is on another file."
        return ""

    def enter_preview(self) -> None:
        """Right on a heading: into the preview, jumping to that heading first
        unless the reader is already within its section."""
        tree = self._tree()
        index = tree.cursor_index() if tree is not None else None
        if tree is not None and index is not None:
            reading_here = self._reader is not None and tree.outline.within(index, self._reader)
            if not reading_here and not self.jump(index):
                return
        self._app.bridge_to_preview(f"#{PANE_ID}")


def _same_place(a: OutlineEntry, b: OutlineEntry) -> bool:
    """Both entries hold the same stretch of the document, so either may keep
    the cursor (a PDF bookmark and the parent synthesised on its page)."""
    return a.chunk_seq == b.chunk_seq and (a.ordinal == b.ordinal or (a.at_top and b.at_top))

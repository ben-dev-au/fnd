"""Results-pane tree widget."""

from __future__ import annotations

from typing import Any, ClassVar

from textual import events
from textual.binding import Binding, BindingType
from textual.message import Message
from textual.widgets import Tree
from textual.widgets.tree import TreeNode

__all__ = ["ResultsTree"]


class ResultsTree(Tree[dict[str, Any]]):
    """Results tree where expanded parents (file rows) are literally
    unselectable.

    Also owns "scan mode": Option/Alt + ↑/↓ move the cursor WITHOUT loading the
    preview (browse fast with no mount per row); a normal ↑/↓ ends scan mode so
    the preview loads where you land, and Enter loads the highlighted row
    (wired in the app). Handling this in the tree's own actions — rather than a
    bubbled ``app.on_key`` — is reliable: the focused tree always runs them,
    where a key the tree consumes may never reach the app.

    Earlier the rule was enforced after-the-fact by ``_on_tree_highlight``
    and ``_bounce_after_expand``: the cursor would land on the parent row
    for a frame and then bounce. With a slow preview load on top, the
    bounce became visible and felt like a glitchy jump.

    Validating in :meth:`validate_cursor_line` is atomic — the cursor
    never lands on an expanded parent in the first place. Pressing ↓
    from the row above an expanded parent moves directly to the parent's
    first child; pressing ↑ from a child moves directly to the row above
    the parent. No frames in between.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("alt+down", "scan_cursor_down", "Scan down", show=False),
        Binding("alt+up", "scan_cursor_up", "Scan up", show=False),
    ]

    class ReopenRequested(Message):
        """Posted when the pane is clicked while collapsed-to-header. The app
        reopens the panel (and expands the clicked node) — a toggle the user
        can't see is useless, so the click should surface content instead."""

        def __init__(self, tree: ResultsTree, node: TreeNode[Any] | None) -> None:
            self.tree = tree
            self.node = node
            super().__init__()

    def on_resize(self, _event: events.Resize) -> None:
        # While collapsed-to-header the pane shows a single content row; keep
        # the cursor (the file driving the preview) parked in it, else the
        # strip snaps back to the top result. Fires when add-class shrinks the
        # pane, so no post-collapse scroll timing to guess at.
        if self.id == "results_pane" and "collapsed" in self.classes and self.cursor_line >= 0:
            self.scroll_to_line(self.cursor_line, animate=False)

    async def _on_click(self, event: events.Click) -> None:
        # Collapsed-to-header: the only visible row is the selected file. A
        # click there should reopen the pane (and expand that result), not
        # toggle a node hidden behind the collapsed height.
        if "collapsed" in self.classes:
            meta = event.style.meta
            line = meta.get("line")
            node = self.get_node_at_line(line) if isinstance(line, int) else None
            self.post_message(self.ReopenRequested(self, node))
            event.stop()
            return
        await super()._on_click(event)

    def _set_scan(self, scanning: bool) -> None:
        # Scan mode drives the PREVIEW, so only the results pane owns it.
        # ResultsTree is also used for the Filters panel (app.py) — Option+arrow
        # there must not flip the preview's scan flag and suppress a later load.
        if self.id != "results_pane":
            return
        preview = getattr(self.app, "_preview", None)
        if preview is not None:
            preview._scan_move = scanning

    def on_key(self, event: events.Key) -> None:
        # Any non-scan key ends scan mode — not just ↑/↓ but home/end/pageup/
        # pagedown/typing too — so a later move always loads instead of being
        # silently suppressed. on_key runs before the key's binding, so the
        # cursor move that follows isn't treated as a scan.
        if event.key not in ("alt+up", "alt+down"):
            self._set_scan(False)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        # Clicking a result is a deliberate selection, never a silent scan —
        # otherwise mouse users could get stuck with the preview not updating.
        self._set_scan(False)

    def on_blur(self, event: events.Blur) -> None:
        # Leaving the tree ends scan mode so it can't strand a later load.
        self._set_scan(False)

    def action_cursor_down(self) -> None:
        # A normal move ends scan mode so the preview loads where you land.
        self._set_scan(False)
        super().action_cursor_down()

    def action_cursor_up(self) -> None:
        self._set_scan(False)
        # In the filters pane, Up from the top row focuses the docked clear bar
        # above the tree (when shown), so the bar is keyboard-reachable like any
        # row. Everywhere else this is a normal cursor move.
        if self.id == "filters_panel_tree" and int(self.cursor_line) <= 0:
            try:
                bar = self.app.query_one("#clear_filters_bar")
            except Exception:
                bar = None
            if bar is not None and bar.display:
                bar.focus()
                return
        super().action_cursor_up()

    def action_scan_cursor_down(self) -> None:
        # Option/Alt+Down: browse without loading the preview.
        self._set_scan(True)
        super().action_cursor_down()

    def action_scan_cursor_up(self) -> None:
        self._set_scan(True)
        super().action_cursor_up()

    def _is_selectable_when_expanded(self, node: Any) -> bool:
        """Whether an expanded parent should still accept the cursor.

        Default False, which keeps the historic behaviour: results and
        filter-category headers are dead rows when open, so the cursor skips
        past them rather than parking where Enter does nothing. Rows that
        carry their own selectable payload override this.
        """
        data = node.data if isinstance(node.data, dict) else None
        return bool(data) and data.get("kind") == "filter_value"

    def validate_cursor_line(self, value: int) -> int:
        clamped = super().validate_cursor_line(value)
        if not getattr(self, "_skip_expanded_parents", False):
            return clamped
        # Walk in the move direction past any expanded parents.
        current = int(self.cursor_line)
        direction = 1 if clamped > current else (-1 if clamped < current else 1)
        last = max(0, len(self._tree_lines) - 1)
        target = clamped
        safety = 0
        while safety < 64:
            try:
                line = self._tree_lines[target]
            except IndexError:
                return clamped
            node = line.node
            if node is self.root:
                return target
            if not (node.children and node.is_expanded):
                return target
            # An expanded parent that is itself selectable stays reachable.
            # Nested tag rows are real, selectable tags that happen to have
            # children; skipping them would make Enter unable to ever toggle
            # a tag whose subtree is open.
            if self._is_selectable_when_expanded(node):
                return target
            next_target = target + direction
            if next_target < 0 or next_target > last:
                # Boundary — don't shove the cursor off the edge; keep
                # it where it was so the press feels like a no-op
                # instead of a jump.
                return current
            target = next_target
            safety += 1
        return current

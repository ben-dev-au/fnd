"""Results-pane tree widget."""

from __future__ import annotations

from typing import Any, ClassVar

from textual.binding import Binding, BindingType
from textual.widgets import Tree

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

    def _set_scan(self, scanning: bool) -> None:
        preview = getattr(self.app, "_preview", None)
        if preview is not None:
            preview._scan_move = scanning

    def action_cursor_down(self) -> None:
        # A normal move ends scan mode so the preview loads where you land.
        self._set_scan(False)
        super().action_cursor_down()

    def action_cursor_up(self) -> None:
        self._set_scan(False)
        super().action_cursor_up()

    def action_scan_cursor_down(self) -> None:
        # Option/Alt+Down: browse without loading the preview.
        self._set_scan(True)
        super().action_cursor_down()

    def action_scan_cursor_up(self) -> None:
        self._set_scan(True)
        super().action_cursor_up()

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
            next_target = target + direction
            if next_target < 0 or next_target > last:
                # Boundary — don't shove the cursor off the edge; keep
                # it where it was so the press feels like a no-op
                # instead of a jump.
                return current
            target = next_target
            safety += 1
        return current

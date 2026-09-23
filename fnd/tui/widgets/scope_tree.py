"""Sidebar tree that keeps its cursor on screen."""

from __future__ import annotations

from typing import Any, ClassVar

from textual import events
from textual.binding import Binding, BindingType
from textual.widgets import Tree

from fnd.tui.widgets.arrow_expansion import ArrowsExpand, HomeToFirstRow

__all__ = ["ScopeTree"]


class ScopeTree(ArrowsExpand, HomeToFirstRow, Tree[dict[str, Any]]):
    """A ``Tree`` whose highlighted row survives a change of viewport.

    Every search re-lays the sidebar out (results arriving and leaving change
    the panel's height), and Textual clamps the scroll offset without moving
    the cursor. The highlighted row then sits outside the visible window until
    the next keypress snaps back to it, which reads as the panel having jumped
    to the top on its own.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("home", "cursor_first", "First row", show=False),
    ]

    def on_resize(self, _event: events.Resize) -> None:
        if self.cursor_line >= 0:
            self.call_after_refresh(self._keep_cursor_visible)

    def _keep_cursor_visible(self) -> None:
        line = self.cursor_line
        if line < 0:
            return
        top = self.scroll_offset.y
        height = self.size.height
        if height and top <= line < top + height:
            return
        self.scroll_to_line(line, animate=False)

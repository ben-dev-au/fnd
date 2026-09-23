"""Key behaviour shared by every tree fnd defines."""

from __future__ import annotations

from typing import Any

__all__ = ["ArrowsExpand", "HomeToFirstRow"]


class ArrowsExpand:
    """Mixin for a ``Tree``: Textual's stock ``space`` → ``toggle_node`` is off.

    On a ``ToggleTree`` row space toggles the selection, so a second expand key
    on space would do two different things in two panes of the same screen.
    """

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "toggle_node":
            return None
        return super().check_action(action, parameters)  # type: ignore[misc]


#: How far down to look for a row the cursor may rest on. A header run longer
#: than this is not a tree anyone is navigating with Home.
_FIRST_ROW_SCAN = 64


class HomeToFirstRow:
    """Mixin for a ``Tree``: ``home`` moves the cursor to the first row.

    The scroll view's own ``home`` binding scrolls a viewport the cursor does
    not follow, so the pane looks frozen.
    """

    def action_cursor_first(self) -> None:
        tree: Any = self
        if not tree.root.children:
            return
        # `validate_cursor_line` owns what is selectable, and the results tree
        # refuses an expanded parent BY DIRECTION (an upward move onto line 0
        # is a no-op), so walk down until it accepts a line.
        for line in range(min(_FIRST_ROW_SCAN, len(tree._tree_lines))):
            tree.cursor_line = line
            if tree.cursor_line == line:
                break
        # To the top of the list, not to the cursor: the first row may be an
        # expanded parent the cursor cannot rest on, and leaving it just off
        # screen is not "Home".
        tree.scroll_to_line(0, animate=False)

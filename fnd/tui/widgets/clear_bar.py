"""Focusable clear-filters bar docked at the top of the filters pane.

A plain Static can't be reached by the keyboard, so the bar is its own
focusable widget: Up from the top of the filters tree focuses it, Enter clears,
and Down returns to the tree. It stays clickable, and the X action still clears
from anywhere.
"""

from __future__ import annotations

import contextlib

from textual import events
from textual.widgets import Static


class ClearFiltersBar(Static):
    can_focus = True

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            self.app._scope.clear_filters()  # type: ignore[attr-defined]
            # clear_filters hid the bar; move focus back to the tree.
            self._focus_tree()
        elif event.key in ("down", "up"):
            event.stop()
            self._focus_tree()

    def _focus_tree(self) -> None:
        with contextlib.suppress(Exception):
            self.app.query_one("#filters_panel_tree").focus()

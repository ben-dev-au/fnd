"""Search field atop a sidebar pane, narrowing its tree to the rows that match.

The host rebuilds the tree from its model on every change and calls
:func:`prune` after, so a search never touches scope or the persisted expand
state. A row matches on ``data["text"]``; a row without one only ever shows as
the parent of a match. A matching row keeps its whole subtree, and a row kept
for a match below it opens.
"""

from __future__ import annotations

import contextlib
import weakref
from typing import TYPE_CHECKING, Any

from rich.style import Style
from textual import events
from textual.message import Message
from textual.widgets import Input

from fnd.text_canon import fold

if TYPE_CHECKING:
    from rich.text import Text
    from textual.widget import Widget
    from textual.widgets import Tree
    from textual.widgets.tree import TreeNode

__all__ = [
    "PaneSearch",
    "SearchHighlightLabel",
    "focus_pane_search",
    "opened_by_search",
    "prune",
    "row_matches",
]

_MATCH_STYLE = Style(bold=True)
# The Settings filter's wording; the key shows only while it is not typed into.
_PLACEHOLDER = "Filter rows…"
_PLACEHOLDER_WITH_KEY = f"{_PLACEHOLDER}  (^/)"


# NodeExpanded is handled after it is posted, by when the search may have
# cleared; the node itself records who opened it.
_OPENED_BY_SEARCH: weakref.WeakSet[TreeNode[Any]] = weakref.WeakSet()


def opened_by_search(node: TreeNode[Any]) -> bool:
    return node in _OPENED_BY_SEARCH


def row_matches(data: object, folded_query: str) -> bool:
    return isinstance(data, dict) and folded_query in fold(str(data.get("text", "")))


def prune(tree: Tree[Any]) -> None:
    """Drop every row outside the tree's ``search_query``; a blank one keeps them all."""
    folded = fold(str(getattr(tree, "search_query", "")).strip())
    setattr(tree, "shown_query", folded)  # noqa: B010, a plain Tree has no such slot
    if folded:
        _prune(tree.root, folded, keep=False)


def _prune(node: TreeNode[Any], folded: str, *, keep: bool) -> bool:
    found = False
    for child in list(node.children):
        hit = row_matches(child.data, folded)
        below = _prune(child, folded, keep=keep or hit)
        if below and not child.is_expanded:
            _OPENED_BY_SEARCH.add(child)
            child.expand()
        if hit or below:
            found = True
        elif not keep:
            child.remove()
    return found


def focus_pane_search(tree: Widget) -> bool:
    """Focus the search field above ``tree``, if its pane has one."""
    parent = tree.parent
    if parent is None or "collapsed" in parent.classes:
        return False
    for widget in parent.children:
        if isinstance(widget, PaneSearch) and widget.tree_id == tree.id and widget.display:
            widget.focus()
            return True
    return False


class SearchHighlightLabel:
    """Mixin for a ``Tree``: the pane search's text reads bold in a matching row."""

    search_query: str = ""
    #: The query the rows were last pruned for, so a rebuild can tell a
    #: search's opened branches from the user's.
    shown_query: str = ""

    def render_label(self, node: TreeNode[Any], base_style: Style, style: Style) -> Text:
        rendered = super().render_label(node, base_style, style)  # type: ignore[misc]
        folded = fold(self.search_query.strip())
        if folded and row_matches(node.data, folded):
            plain = fold(rendered.plain)
            # Folding can change a string's length; a span then lands off the text.
            at = plain.find(folded) if len(plain) == len(rendered.plain) else -1
            if at >= 0:
                rendered.stylize(_MATCH_STYLE, at, at + len(folded))
        return rendered


class PaneSearch(Input):
    """The field itself; its keys are documented on the pane's footer hints."""

    class CollapseRequested(Message):
        """Left at the start of the field: fold the pane to its header."""

        def __init__(self, search: PaneSearch) -> None:
            self.search = search
            super().__init__()

    def __init__(self, *, tree_id: str, id: str) -> None:
        super().__init__(placeholder=_PLACEHOLDER_WITH_KEY, id=id, classes="pane-search")
        self.tree_id = tree_id

    def on_focus(self) -> None:
        self.placeholder = _PLACEHOLDER

    def on_blur(self) -> None:
        self.placeholder = _PLACEHOLDER_WITH_KEY

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            self._stop(event)
            self._enter_tree()
        elif event.key == "down":
            self._stop(event)
            below = self._next_below()
            if below is None:
                self._enter_tree()
            else:
                below.focus()
        elif event.key == "left" and self.cursor_position == 0:
            self._stop(event)
            self.post_message(self.CollapseRequested(self))
        elif event.key == "escape" and self.value:
            self._stop(event)
            self.value = ""

    @staticmethod
    def _stop(event: events.Key) -> None:
        event.stop()
        event.prevent_default()

    def _next_below(self) -> Any | None:
        """The first row between the field and the tree that is showing, if any."""
        parent = self.parent
        if parent is None:
            return None
        siblings = list(parent.children)
        for widget in siblings[siblings.index(self) + 1 :]:
            if widget.id == self.tree_id:
                return None
            if widget.can_focus and widget.display and widget.visible:
                return widget
        return None

    def _enter_tree(self) -> None:
        with contextlib.suppress(Exception):
            tree: Any = self.screen.query_one(f"#{self.tree_id}")
            tree.focus()
            tree.action_cursor_first()

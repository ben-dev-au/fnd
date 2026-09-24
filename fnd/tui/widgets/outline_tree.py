"""Outline-pane tree widget."""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual import events, on
from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from fnd.tui.outline_model import Outline, OutlineEntry
from fnd.tui.widgets.results_tree import ResultsTree

__all__ = ["OutlineTree"]


class OutlineTree(ResultsTree):
    """One row per heading of the document in the preview.

    Enter or a click is the jump (``NodeSelected``, handled by the app); moving
    the cursor never touches the preview, so ``NodeHighlighted`` stops here.
    Nodes carry ``{"kind": "heading", "index": i}`` into :attr:`outline`.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.auto_expand = False
        self._skip_expanded_parents = False
        self.outline = Outline()
        self._heading_nodes: list[TreeNode[dict[str, Any]]] = []

    def on_mount(self) -> None:
        self.show_root = False
        self.guide_depth = 2

    @on(Tree.NodeHighlighted)
    def _keep_highlight_local(self, event: Tree.NodeHighlighted[dict[str, Any]]) -> None:
        event.stop()

    posts_geometry: ClassVar[bool] = False

    def on_resize(self, _event: events.Resize) -> None:
        if self.cursor_line >= 0:
            self.call_after_refresh(self._keep_cursor_visible)

    def _keep_cursor_visible(self) -> None:
        line = self.cursor_line
        top = self.scroll_offset.y
        if line < 0 or (self.size.height and top <= line < top + self.size.height):
            return
        self.scroll_to_line(line, animate=False)

    def set_outline(self, outline: Outline, placeholder: str = "") -> None:
        """Show ``outline``, top level expanded; ``placeholder`` (or the
        outline's reason) when it has no entries."""
        self.outline = outline
        self._heading_nodes = []
        self.clear()
        entries = outline.entries
        if not entries:
            self.root.add_leaf(Text(placeholder or outline.reason, style="dim"))
            self.unselect()
            return
        parents: list[TreeNode[dict[str, Any]]] = [self.root]
        for i, entry in enumerate(entries):
            del parents[entry.depth + 1 :]
            parent = parents[-1]
            data = {"kind": "heading", "index": i}
            has_children = i + 1 < len(entries) and entries[i + 1].depth > entry.depth
            if has_children:
                node = parent.add(_label(entry), data=data, expand=entry.depth == 0)
                parents.append(node)
            else:
                node = parent.add_leaf(_label(entry), data=data)
            self._heading_nodes.append(node)
        self.unselect()

    def cursor_index(self) -> int | None:
        """Index into ``outline.entries`` of the row under the cursor."""
        node = self.cursor_node if self.cursor_line >= 0 else None
        data = node.data if node is not None else None
        return data.get("index") if isinstance(data, dict) else None

    def place_cursor(self, index: int | None) -> None:
        """Put the cursor on entry ``index``, or on its nearest visible
        ancestor when a collapsed heading hides it. Never expands anything."""
        if index is None or not 0 <= index < len(self._heading_nodes):
            self.unselect()
            return
        node = _deepest_visible(self._heading_nodes[index])
        _ = self._tree_lines  # builds the lines, so node.line is current
        if self.cursor_line < 0 or node is not self.cursor_node:
            self.move_cursor(node)


def _label(entry: OutlineEntry) -> Text:
    # Text, never str: a str label is parsed as Rich markup.
    label = Text(entry.title)
    if entry.locator:
        label.append(f"  {entry.locator}", style="dim")
    return label


def _deepest_visible(node: TreeNode[Any]) -> TreeNode[Any]:
    chain: list[TreeNode[Any]] = []
    walk: TreeNode[Any] | None = node.parent
    while walk is not None and walk.parent is not None:
        chain.append(walk)
        walk = walk.parent
    for ancestor in reversed(chain):
        if not ancestor.is_expanded:
            return ancestor
    return node

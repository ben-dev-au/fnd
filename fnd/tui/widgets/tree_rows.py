"""The rows a Textual ``Tree`` shows, read through its public API only."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from textual.widgets import Tree
from textual.widgets.tree import TreeNode

__all__ = ["line_of", "shown_rows"]


def shown_rows(tree: Tree[Any]) -> Iterator[tuple[int, TreeNode[Any]]]:
    """``(line, node)`` for each row ``tree`` shows, top to bottom. ``node.line`` is
    no substitute: a rebuild leaves a node that collapsed out of view its old line."""
    for line in range(tree.last_line + 1):
        node = tree.get_node_at_line(line)
        if node is not None:
            yield line, node


def line_of(tree: Tree[Any], node: TreeNode[Any]) -> int | None:
    """The line ``node`` shows on, or None while it is not shown."""
    return next((line for line, shown in shown_rows(tree) if shown is node), None)

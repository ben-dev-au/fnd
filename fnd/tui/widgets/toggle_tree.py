"""Reusable nested tri-state toggle tree.

One correct implementation of "categories of toggleable items, with ●/◐/○
tri-state and parent↔child cascade" so the file-type filter and the
source-creation Includes picker share identical, bug-free behaviour instead of
each re-deriving it (and re-deriving the same bugs).

Correct by construction — each property kills a class of bug the ad-hoc trees hit:

* ``auto_expand = False`` and Enter/Space = *toggle only*; ←/→ = expand/collapse.
  Toggling a category can never also expand/collapse it.
* Every node is selectable, so a mouse click toggles whatever row it lands on
  (no "click registers but nothing happens").
* Toggling repaints only the affected markers IN PLACE (never ``clear()`` +
  rebuild), so the cursor never jumps.

The host owns the model (:class:`ToggleGroup`s) and reacts to
:class:`ToggleTree.SelectionChanged`; the widget owns rendering + interaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from textual import on
from textual.binding import Binding, BindingType
from textual.message import Message
from textual.widgets import Tree
from textual.widgets.tree import TreeNode

_FULL = "●"
_PARTIAL = "◐"
_EMPTY = "○"
_MARKER_GAP = "  "


def tri_state_marker(n_selected: int, n_total: int) -> str:
    """●/◐/○ for a parent whose children are ``n_selected`` of ``n_total`` on."""
    if n_total == 0 or n_selected == 0:
        return _EMPTY
    return _FULL if n_selected >= n_total else _PARTIAL


@dataclass(frozen=True)
class ToggleItem:
    """A leaf toggle. ``id`` is the value reported in the selection set."""

    id: str
    label: str


@dataclass(frozen=True)
class ToggleGroup:
    """A category whose row toggles all its items and shows their tri-state."""

    id: str
    label: str
    items: tuple[ToggleItem, ...]


class ToggleTree(Tree[dict[str, Any]]):
    """A ``Tree`` of category → item toggles with tri-state parents."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "toggle_selection", "Toggle", show=False),
        Binding("space", "toggle_selection", "Toggle", show=False),
        Binding("right", "expand_here", "Expand", show=False),
        Binding("left", "collapse_here", "Collapse", show=False),
    ]

    class SelectionChanged(Message):
        """Posted after any user toggle. ``selected`` is the full item-id set."""

        def __init__(self, toggle_tree: ToggleTree, selected: frozenset[str]) -> None:
            self.toggle_tree = toggle_tree
            self.selected = selected
            super().__init__()

        @property
        def control(self) -> ToggleTree:
            return self.toggle_tree

    def __init__(self, label: str = "", *, id: str | None = None) -> None:
        super().__init__(label, id=id)
        self.show_root = False
        # Enter/click must never auto-expand a branch (that is Bug: "Enter also
        # expands the category"). Textual's Tree auto-expands on NodeSelected
        # when this is True.
        self.auto_expand = False
        self._groups: tuple[ToggleGroup, ...] = ()
        self._selected: set[str] = set()
        self._item_labels: dict[str, str] = {}

    # ── Model ────────────────────────────────────────────────────────────
    def set_model(
        self,
        groups: list[ToggleGroup] | tuple[ToggleGroup, ...],
        selected: set[str] | frozenset[str],
        *,
        expanded: set[str] | None = None,
    ) -> None:
        """Replace the tree contents. ``expanded`` = group ids to open."""
        self._groups = tuple(groups)
        self._selected = set(selected)
        self._item_labels = {it.id: it.label for g in self._groups for it in g.items}
        self._rebuild(expanded or set())

    @property
    def selected(self) -> frozenset[str]:
        return frozenset(self._selected)

    @property
    def expanded_group_ids(self) -> set[str]:
        """Group ids currently expanded — for the host to persist."""
        out: set[str] = set()
        for node in self.root.children:
            data = node.data if isinstance(node.data, dict) else {}
            if data.get("kind") == "group" and node.is_expanded:
                out.add(str(data.get("id")))
        return out

    def _rebuild(self, expanded: set[str]) -> None:
        self.clear()
        for g in self._groups:
            gnode = self.root.add(
                self._group_label(g),
                data={"kind": "group", "id": g.id},
                expand=g.id in expanded,
            )
            for it in g.items:
                gnode.add_leaf(
                    self._item_label(it.id),
                    data={"kind": "item", "id": it.id, "group": g.id},
                )

    # ── Labels ───────────────────────────────────────────────────────────
    def _group_label(self, g: ToggleGroup) -> str:
        n = sum(1 for it in g.items if it.id in self._selected)
        return f"{tri_state_marker(n, len(g.items))}{_MARKER_GAP}{g.label}"

    def _item_label(self, item_id: str) -> str:
        marker = _FULL if item_id in self._selected else _EMPTY
        return f"{marker}{_MARKER_GAP}{self._item_labels.get(item_id, item_id)}"

    # ── Toggle ───────────────────────────────────────────────────────────
    def action_toggle_selection(self) -> None:
        node = self.cursor_node
        if node is not None:
            self._toggle(node)

    @on(Tree.NodeSelected)
    def _on_node_selected(self, ev: Tree.NodeSelected[dict[str, Any]]) -> None:
        # A mouse click routes here (Tree._on_click → select_cursor → NodeSelected).
        # Handle it as a toggle and stop it so the app's generic NodeSelected
        # shims don't also react.
        ev.stop()
        self._toggle(ev.node)

    def _toggle(self, node: TreeNode[dict[str, Any]]) -> None:
        data = node.data if isinstance(node.data, dict) else {}
        kind = data.get("kind")
        if kind == "group":
            g = self._group_by_id(str(data.get("id")))
            if g is None:
                return
            ids = {it.id for it in g.items}
            if ids and ids <= self._selected:
                self._selected -= ids
            else:
                self._selected |= ids
            self._repaint_group(node, g)
        elif kind == "item":
            item_id = str(data.get("id"))
            self._selected.symmetric_difference_update({item_id})
            node.set_label(self._item_label(item_id))
            self._repaint_parent(node)
        else:
            return
        self.post_message(self.SelectionChanged(self, self.selected))

    def _repaint_group(self, gnode: TreeNode[dict[str, Any]], g: ToggleGroup) -> None:
        gnode.set_label(self._group_label(g))
        for child in gnode.children:
            cdata = child.data if isinstance(child.data, dict) else {}
            child.set_label(self._item_label(str(cdata.get("id"))))

    def _repaint_parent(self, node: TreeNode[dict[str, Any]]) -> None:
        parent = node.parent
        if parent is None:
            return
        pdata = parent.data if isinstance(parent.data, dict) else {}
        g = self._group_by_id(str(pdata.get("id")))
        if g is not None:
            parent.set_label(self._group_label(g))

    def _group_by_id(self, gid: str) -> ToggleGroup | None:
        return next((g for g in self._groups if g.id == gid), None)

    # ── Expand / collapse (←/→) ──────────────────────────────────────────
    def action_expand_here(self) -> None:
        node = self.cursor_node
        if node is not None and node.allow_expand and not node.is_expanded:
            node.expand()

    def action_collapse_here(self) -> None:
        node = self.cursor_node
        if node is None:
            return
        if node.allow_expand and node.is_expanded:
            node.collapse()
        elif node.parent is not None and node.parent is not self.root:
            # On a leaf: collapse toward the parent group, standard tree feel.
            for line, tl in enumerate(self._tree_lines):
                if tl.node is node.parent:
                    self.cursor_line = line
                    break

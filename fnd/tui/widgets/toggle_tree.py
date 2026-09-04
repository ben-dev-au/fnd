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
_EXCLUDED = "⊘"
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
    """A category whose row toggles all its items and shows their tri-state.

    ``mode`` picks the leaf behaviour, so one tree can carry the several kinds
    of choice a filter set needs:

    * ``multi``   — any number on (file types)
    * ``cycle``   — off → include → exclude → off, as the query pane's tags do
    * ``radio``   — at most one on (a date window, a size bound)
    * ``actions`` — leaves carry no state; Enter asks the host to open an
      editor. For the rules that are typed rather than ticked.

    ``empty_label`` is what the branch means when nothing under it is on —
    "no file type ticked" reads as *nothing included* unless the row says
    otherwise.
    """

    id: str
    label: str
    items: tuple[ToggleItem, ...]
    mode: str = "multi"
    empty_label: str = ""
    groups: tuple[ToggleGroup, ...] = ()
    """Sub-categories. A group carries items or sub-groups, not usually both."""

    @property
    def leaves(self) -> tuple[ToggleItem, ...]:
        """Every item at or below this group."""
        return self.items + tuple(it for g in self.groups for it in g.leaves)

    def walk(self) -> tuple[ToggleGroup, ...]:
        return (self, *(d for g in self.groups for d in g.walk()))


class ToggleTree(Tree[dict[str, Any]]):
    """A ``Tree`` of category → item toggles with tri-state parents."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "toggle_selection", "Toggle", show=False),
        Binding("space", "toggle_selection", "Toggle", show=False),
        Binding("right", "expand_here", "Expand", show=False),
        Binding("left", "collapse_here", "Collapse", show=False),
    ]

    class ActionSelected(Message):
        """Enter on an ``actions`` leaf: the host opens that rule's editor."""

        def __init__(self, toggle_tree: ToggleTree, item_id: str) -> None:
            self.toggle_tree = toggle_tree
            self.item_id = item_id
            super().__init__()

        @property
        def control(self) -> ToggleTree:
            return self.toggle_tree

    class NavigatedOut(Message):
        """← pressed with nothing left to collapse: the host should go back."""

        def __init__(self, toggle_tree: ToggleTree) -> None:
            self.toggle_tree = toggle_tree
            super().__init__()

        @property
        def control(self) -> ToggleTree:
            return self.toggle_tree

    class SelectionChanged(Message):
        """Posted after any user toggle. ``selected`` is the full item-id set;
        ``excluded`` is populated only in cycle mode."""

        def __init__(
            self,
            toggle_tree: ToggleTree,
            selected: frozenset[str],
            excluded: frozenset[str] = frozenset(),
        ) -> None:
            self.toggle_tree = toggle_tree
            self.selected = selected
            self.excluded = excluded
            super().__init__()

        @property
        def control(self) -> ToggleTree:
            return self.toggle_tree

    def __init__(
        self, label: str = "", *, id: str | None = None, cycle_leaves: bool = False
    ) -> None:
        super().__init__(label, id=id)
        # Cycle mode gives leaves a third state — ⊘ exclude — matching the
        # Filters pane's tag rows, where "not selected" and "actively
        # excluded" are different answers.
        self._cycle_leaves = cycle_leaves
        self._excluded: set[str] = set()
        self.show_root = False
        # Enter/click must never auto-expand a branch (that is Bug: "Enter also
        # expands the category"). Textual's Tree auto-expands on NodeSelected
        # when this is True.
        self.auto_expand = False
        self._groups: tuple[ToggleGroup, ...] = ()
        self._by_id: dict[str, ToggleGroup] = {}
        self._action_items: set[str] = set()
        self._selected: set[str] = set()
        self._item_labels: dict[str, str] = {}

    # ── Model ────────────────────────────────────────────────────────────
    def set_model(
        self,
        groups: list[ToggleGroup] | tuple[ToggleGroup, ...],
        selected: set[str] | frozenset[str],
        *,
        excluded: set[str] | frozenset[str] | None = None,
        expanded: set[str] | None = None,
    ) -> None:
        """Replace the tree contents. ``expanded`` = group ids to open."""
        self._groups = tuple(groups)
        self._by_id = {d.id: d for g in self._groups for d in g.walk()}
        self._selected = set(selected)
        self._excluded = set(excluded or ())
        self._item_labels = {it.id: it.label for g in self._groups for it in g.leaves}
        self._action_items = {
            it.id for g in self._by_id.values() if g.mode == "actions" for it in g.items
        }
        self._rebuild(expanded or set())

    @property
    def selected(self) -> frozenset[str]:
        return frozenset(self._selected)

    @property
    def excluded(self) -> frozenset[str]:
        return frozenset(self._excluded)

    @property
    def expanded_group_ids(self) -> set[str]:
        """Group ids currently expanded, at any depth — for the host to persist."""
        out: set[str] = set()
        stack = list(self.root.children)
        while stack:
            node = stack.pop()
            data = node.data if isinstance(node.data, dict) else {}
            if data.get("kind") == "group" and node.is_expanded:
                out.add(str(data.get("id")))
            stack.extend(node.children)
        return out

    def _rebuild(self, expanded: set[str]) -> None:
        self.clear()
        for g in self._groups:
            self._add_group(self.root, g, expanded)

    def _add_group(
        self, parent: TreeNode[dict[str, Any]], g: ToggleGroup, expanded: set[str]
    ) -> None:
        gnode = parent.add(
            self._group_label(g),
            data={"kind": "group", "id": g.id},
            expand=g.id in expanded,
        )
        for sub in g.groups:
            self._add_group(gnode, sub, expanded)
        for it in g.items:
            gnode.add_leaf(
                self._item_label(it.id),
                data={"kind": "item", "id": it.id, "group": g.id},
            )

    # ── Labels ───────────────────────────────────────────────────────────
    def _group_label(self, g: ToggleGroup) -> str:
        mode = self._mode(g)
        leaves = g.leaves
        if mode == "actions":
            return f"{_MARKER_GAP}{g.label}"
        if mode == "cycle":
            n_ex = sum(1 for it in leaves if it.id in self._excluded)
            if n_ex:
                # ⊘ only when the whole branch is excluded; a single excluded
                # tag among many is a partial state, not a blanket exclusion.
                return f"{_EXCLUDED if n_ex == len(leaves) else _PARTIAL}{_MARKER_GAP}{g.label}"
        n = sum(1 for it in leaves if it.id in self._selected)
        if not n and g.empty_label:
            return f"{_EMPTY}{_MARKER_GAP}{g.label}  ({g.empty_label})"
        if mode == "radio":
            chosen = next((it for it in leaves if it.id in self._selected), None)
            # The "any" option is the absence of a filter, so the branch reads
            # as unset — a ● there says a bound is active when none is.
            active = chosen is not None and not chosen.id.endswith(":any")
            marker = _FULL if active else _EMPTY
            suffix = f"  ({chosen.label})" if chosen else "  (any)"
            return f"{marker}{_MARKER_GAP}{g.label}{suffix}"
        if not leaves:
            return f"{_EMPTY}{_MARKER_GAP}{g.label}"
        return f"{tri_state_marker(n, len(leaves))}{_MARKER_GAP}{g.label}"

    def _item_label(self, item_id: str) -> str:
        if item_id in self._action_items:
            return f"⏎{_MARKER_GAP}{self._item_labels.get(item_id, item_id)}"
        if item_id in self._excluded:
            marker = _EXCLUDED
        elif item_id in self._selected:
            marker = _FULL
        else:
            marker = _EMPTY
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
            ids = {it.id for it in g.leaves}
            if self._mode(g) == "actions":
                return
            if self._mode(g) in ("cycle", "radio"):
                # Selecting every tag is never what the user means; clearing
                # the branch is.
                self._selected -= ids
                self._excluded -= ids
            elif ids and ids <= self._selected:
                self._selected -= ids
            else:
                self._selected |= ids
            self._repaint_group(node, g)
        elif kind == "item":
            item_id = str(data.get("id"))
            group = self._group_by_id(str(data.get("group")))
            mode = self._mode(group)
            if mode == "actions":
                self.post_message(self.ActionSelected(self, item_id))
                return
            if mode == "cycle":
                self._cycle(item_id)
                node.set_label(self._item_label(item_id))
                self._repaint_parent(node)
            elif mode == "radio" and group is not None:
                self._selected -= {it.id for it in group.leaves if it.id != item_id}
                self._selected.symmetric_difference_update({item_id})
                parent = node.parent
                if parent is not None:
                    self._repaint_group(parent, group)
            else:
                self._selected.symmetric_difference_update({item_id})
                node.set_label(self._item_label(item_id))
                self._repaint_parent(node)
        else:
            return
        self.post_message(self.SelectionChanged(self, self.selected, self.excluded))

    def _cycle(self, item_id: str) -> None:
        """off → include → exclude → off, as the query pane's tags do."""
        if item_id in self._selected:
            self._selected.discard(item_id)
            self._excluded.add(item_id)
        elif item_id in self._excluded:
            self._excluded.discard(item_id)
        else:
            self._selected.add(item_id)

    def _repaint_group(self, gnode: TreeNode[dict[str, Any]], g: ToggleGroup) -> None:
        gnode.set_label(self._group_label(g))
        for child in gnode.children:
            cdata = child.data if isinstance(child.data, dict) else {}
            if cdata.get("kind") == "group":
                sub = self._by_id.get(str(cdata.get("id")))
                if sub is not None:
                    self._repaint_group(child, sub)
            else:
                child.set_label(self._item_label(str(cdata.get("id"))))

    def _repaint_parent(self, node: TreeNode[dict[str, Any]]) -> None:
        """Repaint every ancestor: a nested group's roll-up depends on it."""
        parent = node.parent
        while parent is not None:
            pdata = parent.data if isinstance(parent.data, dict) else {}
            g = self._group_by_id(str(pdata.get("id")))
            if g is None:
                return
            parent.set_label(self._group_label(g))
            parent = parent.parent

    def _group_by_id(self, gid: str) -> ToggleGroup | None:
        return self._by_id.get(gid)

    def _mode(self, group: ToggleGroup | None) -> str:
        if group is None:
            return "cycle" if self._cycle_leaves else "multi"
        if group.mode == "multi" and self._cycle_leaves:
            return "cycle"
        return group.mode

    # ── Expand / collapse (←/→) ──────────────────────────────────────────
    def action_expand_here(self) -> None:
        node = self.cursor_node
        if node is not None and node.allow_expand and not node.is_expanded:
            node.expand()

    def action_collapse_here(self) -> None:
        node = self.cursor_node
        if node is None:
            self.post_message(self.NavigatedOut(self))
            return
        if node.allow_expand and node.is_expanded:
            node.collapse()
        elif node.parent is None or node.parent is self.root:
            # Nothing left to collapse. Without this the binding swallows ←
            # and the host screen's "left = back" never fires.
            self.post_message(self.NavigatedOut(self))
        else:
            # On a leaf: collapse toward the parent group, standard tree feel.
            for line, tl in enumerate(self._tree_lines):
                if tl.node is node.parent:
                    self.cursor_line = line
                    break

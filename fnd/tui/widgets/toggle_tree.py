"""Reusable nested tri-state toggle tree.

One correct implementation of "categories of toggleable items, with ●/◐/○
tri-state and parent↔child cascade" so the file-type filter and the
source-creation Includes picker share identical, bug-free behaviour instead of
each re-deriving it (and re-deriving the same bugs).

Correct by construction — each property kills a class of bug the ad-hoc trees hit:

* ``auto_expand = False`` and Enter = *toggle only*; ←/→ = expand/collapse.
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

from fnd.tui.results_labels import _styled_state_row, state_colour
from fnd.tui.widgets.arrow_expansion import ArrowsExpand, HomeToFirstRow
from fnd.tui.widgets.clear_bar import focus_clear_bar
from fnd.tui.widgets.state_marker import StateMarkerLabel

_FULL = "●"
_PARTIAL = "◐"
_EMPTY = "○"
_EXCLUDED = "⊘"
_MARKER_GAP = "  "

#: The tag whose guard must not be invertible. "Index only files tagged
#: no_index" empties a collection and needs a reindex to undo, and the tag
#: ships excluded, so one press on the state a user finds would do it.
NEVER_ONLY_TAGS = ("no_index",)


def _plural(noun: str, n: int) -> str:
    """`1 tag`, not `1 tags`."""
    return noun[:-1] if n == 1 and noun.endswith("s") else noun


#: Above this many, a list stops being readable at a glance and a count says
#: more. Three is what the Sources line already names ("csv, md, python").
_NAMED_MAX = 3


def _named(values: list[str], n: int, noun: str, *, total: int = 0) -> str:
    """What is on, named where that is shorter than counting it.

    `1 of 40 types` describes the setting; `md` describes what it does.
    """
    if 0 < len(values) <= _NAMED_MAX:
        return ", ".join(values)
    if total:
        return f"{n} of {total} {_plural(noun, total)}"
    return f"{n} {_plural(noun, n)}"


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
    key: str = ""
    """What makes two leaves the same thing to a user. Defaults to the label;
    set it where the label carries something else as well, such as a count."""


@dataclass(frozen=True)
class ToggleGroup:
    """A category whose row toggles all its items and shows their tri-state.

    ``mode`` picks the leaf behaviour, so one tree can carry the several kinds
    of choice a filter set needs:

    * ``multi``: any number on (file types)
    * ``cycle``: off → exclude → include → off; see ``_cycle`` for why
      this differs from the query pane
    * ``radio``: at most one on (a date window, a size bound)
    * ``actions``: leaves carry no state; Enter asks the host to open an
      editor. For the rules that are typed rather than ticked.

    ``empty_label`` is what the branch means when nothing under it is on:
    "no file type ticked" reads as *nothing included* unless the row says
    otherwise.
    """

    id: str
    label: str
    items: tuple[ToggleItem, ...]
    mode: str = "multi"
    empty_label: str = ""
    full_label: str = ""
    """What the branch means with everything on. Set it only where that is not
    simply "all of them": ticking every file type is the same no-restriction
    as ticking none, and `●` alone reads as the opposite."""
    noun: str = ""
    """Plural name of what the leaves are ("types", "tags"). Given one, a
    partly-on branch says how much is on rather than only that some is."""
    complete: bool = True
    """False while the leaves are still being discovered. Until then the only
    tags known are the excluded ones the spec named, so the roll-up must not
    read ⊘, a red "never index any of these" that becomes ◐ when the scan lands."""
    groups: tuple[ToggleGroup, ...] = ()
    """Sub-categories. A group carries items or sub-groups, not usually both."""
    name_leaves: bool = False
    """Name what is on rather than counting it. For a branch of two or three
    short labels, "1 of 2 files" says less than the file's name."""
    elsewhere: str = ""
    """A bound on this dimension the branch cannot show. A radio branch reading
    `○ (Any size)` while a minimum filters is a false statement, not a
    partial one."""
    hidden: tuple[ToggleItem, ...] = ()
    """Leaves a row filter is not showing. They still belong to the branch, so
    a roll-up counted over the visible ones alone would read `● every type`
    with one of forty ticked."""

    @property
    def leaves(self) -> tuple[ToggleItem, ...]:
        """Every item at or below this group, as the tree shows it."""
        return self.items + tuple(it for g in self.groups for it in g.leaves)

    @property
    def counted_leaves(self) -> tuple[ToggleItem, ...]:
        """Every item the branch HAS, shown or not. What a roll-up speaks for;
        toggling still acts on what is on screen.

        Each level holds only the leaves it dropped itself, so nothing is
        counted twice: an inflated denominator is the same defect as a
        shrunken one.
        """
        return self.items + self.hidden + tuple(it for g in self.groups for it in g.counted_leaves)

    def walk(self) -> tuple[ToggleGroup, ...]:
        return (self, *(d for g in self.groups for d in g.walk()))


class ToggleTree(ArrowsExpand, HomeToFirstRow, StateMarkerLabel, Tree[dict[str, Any]]):
    """A ``Tree`` of category → item toggles with tri-state parents."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("home", "cursor_first", "First row", show=False),
        Binding("enter", "toggle_selection", "Toggle", show=False),
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
        # Cycle mode gives leaves a third state (⊘ exclude), matching the
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
        if self.cursor_line < 0:
            # A tree opens with no cursor, so the first ⏎ or ↓ is spent making
            # one and appears to do nothing.
            self.cursor_line = 0

    @property
    def selected(self) -> frozenset[str]:
        return frozenset(self._selected)

    @property
    def excluded(self) -> frozenset[str]:
        return frozenset(self._excluded)

    @property
    def expanded_group_ids(self) -> set[str]:
        """Group ids currently expanded, at any depth, for the host to persist."""
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
    def _branch_summary(self, g: ToggleGroup, mode: str) -> str:
        """What a collapsed branch currently does, in its own words.

        A marker alone says only *that* a branch is partly on; the size and
        date branches already name their choice, so the ticked ones do too.
        """
        if not g.noun:
            return ""

        # Count what the user can tell apart, not rows: one tag is drawn under
        # every source that can carry it. Keyed on `key`, because a label may
        # also carry a per-source file count.
        def _key(item: ToggleItem) -> str:
            return item.key or item.label

        counted = g.counted_leaves
        seen = {_key(it) for it in counted}
        n_on = len({_key(it) for it in counted if it.id in self._selected})
        n_off = len({_key(it) for it in counted if it.id in self._excluded})
        if g.name_leaves:
            on = [it.label for it in counted if it.id in self._selected]
            return f"  ({', '.join(on)})" if on else ""
        # Deduplicated like the counts above: one tag is drawn under every
        # source that can carry it.
        on = sorted({_key(it) for it in counted if it.id in self._selected})
        off = sorted({_key(it) for it in counted if it.id in self._excluded})
        parts = []
        if mode == "cycle":
            if n_on:
                parts.append(f"only {_named(on, n_on, g.noun)}")
            if n_off:
                parts.append(f"{_named(off, n_off, g.noun)} excluded")
        elif n_on and n_on < len(seen):
            # ● already says "all of them"; a count there is noise.
            parts.append(_named(on, n_on, g.noun, total=len(seen)))
        return f"  ({', '.join(parts)})" if parts else ""

    def _group_label(self, g: ToggleGroup) -> Any:
        """A branch row. Its marker is a state too, so it carries the colour.

        Every marker on a collapsed screen is one of these: colouring only the
        leaves would show no colour at all until a branch is expanded.
        """
        mode = self._mode(g)
        # A roll-up speaks for the whole branch; a row filter hides rows, it
        # does not shrink what "all of them" means.
        leaves = g.counted_leaves
        if mode == "actions":
            return f"{_MARKER_GAP}{g.label}"
        n_ex = sum(1 for it in leaves if it.id in self._excluded)
        n = sum(1 for it in leaves if it.id in self._selected)
        summary = self._branch_summary(g, mode)
        if mode == "cycle" and n_ex:
            # ⊘ only when the whole branch is excluded; a single excluded
            # tag among many is a partial state, not a blanket exclusion.
            marker = _EXCLUDED if (g.complete and n_ex == len(leaves)) else _PARTIAL
            return _styled_state_row(
                marker, f"{_MARKER_GAP}{g.label}{summary}", self._state_colour(marker)
            )
        if not n and g.empty_label:
            return f"{_EMPTY}{_MARKER_GAP}{g.label}  ({g.empty_label})"
        if mode == "radio":
            chosen = next((it for it in leaves if it.id in self._selected), None)
            # The "any" option is the absence of a filter, so the branch reads
            # as unset: a ● there says a bound is active when none is.
            active = chosen is not None and not chosen.id.endswith(":any")
            marker = _FULL if active else (_PARTIAL if g.elsewhere else _EMPTY)
            suffix = f"  ({chosen.label})" if chosen else "  (any)"
            if g.elsewhere:
                suffix = f"{suffix[:-1]} · {g.elsewhere})"
            return _styled_state_row(
                marker, f"{_MARKER_GAP}{g.label}{suffix}", self._state_colour(marker)
            )
        if not leaves:
            return f"{_EMPTY}{_MARKER_GAP}{g.label}"
        if n == len(leaves) and g.full_label:
            summary = f"  ({g.full_label})"
        rolled = tri_state_marker(n, len(leaves))
        return _styled_state_row(
            rolled, f"{_MARKER_GAP}{g.label}{summary}", self._state_colour(rolled)
        )

    def _item_label(self, item_id: str) -> Any:
        if item_id in self._action_items:
            return f"⏎{_MARKER_GAP}{self._item_labels.get(item_id, item_id)}"
        if item_id in self._excluded:
            marker, style = _EXCLUDED, self._state_colour(_EXCLUDED)
        elif item_id in self._selected:
            marker, style = _FULL, self._state_colour(_FULL)
        else:
            marker, style = _EMPTY, ""
        label = f"{_MARKER_GAP}{self._item_labels.get(item_id, item_id)}"
        return _styled_state_row(marker, label, style)

    def _state_colour(self, marker: str) -> str:
        """The colour this state marker carries, or none for a neutral one."""
        try:
            variables = self.app.get_css_variables()
        except Exception:
            variables = {}
        return state_colour(marker, variables)

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
                # Expand, like the branches beside it: the legend says Enter
                # opens an editor, which it does on the leaf this reveals.
                node.toggle()
                return
            if self._mode(g) in ("cycle", "radio"):
                # Expand, do not wipe: selecting every tag is never what the
                # user means, and neither is discarding several exclusions in
                # one keypress with no confirmation and no undo.
                node.toggle()
                return
            elif ids and ids <= self._selected:
                self._selected -= ids
            else:
                self._selected |= ids
            self._repaint_group(node, g)
            # Repaints this node and everything under it; the roll-up above it
            # is what goes stale, and without this "File types" keeps its old
            # count for the rest of the session.
            self._repaint_parent(node)
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
                # Re-selecting the current option is a no-op, as a radio group
                # means everywhere: toggling it off would leave nothing selected,
                # a fourth state the legend cannot express.
                self._selected -= {it.id for it in group.leaves if it.id != item_id}
                self._selected.add(item_id)
                parent = node.parent
                if parent is not None:
                    self._repaint_group(parent, group)
                    self._repaint_parent(parent)
            else:
                self._selected.symmetric_difference_update({item_id})
                node.set_label(self._item_label(item_id))
                self._repaint_parent(node)
        else:
            return
        self.post_message(self.SelectionChanged(self, self.selected, self.excluded))

    @staticmethod
    def _skips_include(item_id: str) -> bool:
        """Whether this row may never reach "index ONLY these"."""
        return item_id.startswith("tag:") and item_id.rsplit(":", 1)[-1] in NEVER_ONLY_TAGS

    def _cycle(self, item_id: str) -> None:
        """off → exclude → include → off.

        The query pane cycles include first, where include narrows a search
        and one more press undoes it. Here include means "index only files
        carrying this", so the first press on a tag can take a collection from
        eight files to one and needs a reindex to undo. Exclude is both the
        commoner intent and the recoverable one, so it goes first.

        ``no_index`` skips include entirely: it ships excluded, so include is
        one press from the state a user finds, and "index only the files I
        marked never-index" is not an intent anyone has.
        """
        if item_id in self._excluded:
            self._excluded.discard(item_id)
            if not self._skips_include(item_id):
                self._selected.add(item_id)
        elif item_id in self._selected:
            self._selected.discard(item_id)
        else:
            self._excluded.add(item_id)

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
    def action_cursor_up(self) -> None:
        """Up from the top row reaches the pane's clear bar, as in the sidebar.

        The row is only an affordance if the keyboard can get to it.
        """
        if int(self.cursor_line) <= 0 and focus_clear_bar(self):
            return
        super().action_cursor_up()

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

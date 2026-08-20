"""Results-pane tree widget."""

from __future__ import annotations

from typing import Any, ClassVar

from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding, BindingType
from textual.message import Message
from textual.widgets import Tree
from textual.widgets._tree import TOGGLE_STYLE
from textual.widgets.tree import TreeNode

from fnd.tui.preview.warmth import WarmState

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

    class ReopenRequested(Message):
        """Posted when the pane is clicked while collapsed-to-header. The app
        reopens the panel (and expands the clicked node) — a toggle the user
        can't see is useless, so the click should surface content instead."""

        def __init__(self, tree: ResultsTree, node: TreeNode[Any] | None) -> None:
            self.tree = tree
            self.node = node
            super().__init__()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Per-file warmth, keyed by parent_id. Read on every label render, so
        # it is a plain dict lookup and never a scan of the result groups.
        self.warm_states: dict[str, WarmState] = {}

    def on_resize(self, _event: events.Resize) -> None:
        # While collapsed-to-header the pane shows a single content row; keep
        # the cursor (the file driving the preview) parked in it, else the
        # strip snaps back to the top result. Fires when add-class shrinks the
        # pane, so no post-collapse scroll timing to guess at.
        if self.id == "results_pane" and "collapsed" in self.classes and self.cursor_line >= 0:
            self.scroll_to_line(self.cursor_line, animate=False)

    def _toggle_node(self, node: Any) -> None:
        # Collapsing a node with the cursor somewhere inside its subtree would
        # otherwise strand the cursor: Textual keeps the cursor *line index*
        # across the rebuild, so it lands on whatever row slides up into that
        # line (the section below the one just collapsed). Follow the cursor up
        # onto the node being collapsed instead — that's the row the user is
        # acting on. Captured before the collapse so the check is reliable.
        follow = (
            node.is_expanded
            and self.cursor_node is not None
            and self.cursor_node is not node
            and self._is_ancestor(node, self.cursor_node)
        )
        super()._toggle_node(node)
        if follow:
            self.move_cursor(node)

    @staticmethod
    def _is_ancestor(ancestor: Any, node: Any) -> bool:
        """True if ``ancestor`` is a (strict) ancestor of ``node``."""
        cur = node.parent
        while cur is not None:
            if cur is ancestor:
                return True
            cur = cur.parent
        return False

    async def _on_click(self, event: events.Click) -> None:
        # DO NOT call super()._on_click here. Textual's message pump dispatches
        # a click to EVERY ``_on_click`` found while walking the MRO by naming
        # convention (message_pump._get_dispatch_methods), so the base
        # ``Tree._on_click`` already runs for us. Calling super as well runs it
        # a second time — one physical click then toggles a node twice
        # (expand+collapse = nothing happens, e.g. clicking an expand arrow) or
        # posts ``NodeSelected`` twice (a leaf toggles on then off). Overriding
        # to ADD behaviour is fine; just never re-invoke the base handler.
        #
        # Collapsed-to-header: the only visible row is the selected file. A
        # click there should reopen the pane (and expand that result), not
        # toggle a node hidden behind the collapsed height.
        if "collapsed" in self.classes:
            meta = event.style.meta
            line = meta.get("line")
            node = self.get_node_at_line(line) if isinstance(line, int) else None
            self.post_message(self.ReopenRequested(self, node))
            event.stop()
            return
        # Otherwise fall through: the base Tree._on_click, dispatched separately
        # via the MRO, handles the normal toggle/select exactly once.

    def _set_scan(self, scanning: bool) -> None:
        # Scan mode drives the PREVIEW, so only the results pane owns it.
        # ResultsTree is also used for the Filters panel (app.py) — Option+arrow
        # there must not flip the preview's scan flag and suppress a later load.
        if self.id != "results_pane":
            return
        preview = getattr(self.app, "_preview", None)
        if preview is not None:
            preview._scan_move = scanning

    def on_key(self, event: events.Key) -> None:
        # Any non-scan key ends scan mode — not just ↑/↓ but home/end/pageup/
        # pagedown/typing too — so a later move always loads instead of being
        # silently suppressed. on_key runs before the key's binding, so the
        # cursor move that follows isn't treated as a scan.
        if event.key not in ("alt+up", "alt+down"):
            self._set_scan(False)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        # Clicking a result is a deliberate selection, never a silent scan —
        # otherwise mouse users could get stuck with the preview not updating.
        self._set_scan(False)

    def on_blur(self, event: events.Blur) -> None:
        # Leaving the tree ends scan mode so it can't strand a later load.
        self._set_scan(False)

    def action_cursor_down(self) -> None:
        # A normal move ends scan mode so the preview loads where you land.
        self._set_scan(False)
        super().action_cursor_down()

    def action_cursor_up(self) -> None:
        self._set_scan(False)
        # In the filters pane, Up from the top row focuses the docked clear bar
        # above the tree (when shown), so the bar is keyboard-reachable like any
        # row. Everywhere else this is a normal cursor move.
        if self.id == "filters_panel_tree" and int(self.cursor_line) <= 0:
            try:
                bar = self.app.query_one("#clear_filters_bar")
            except Exception:
                bar = None
            if bar is not None and bar.visible:
                bar.focus()
                return
        super().action_cursor_up()

    def action_scan_cursor_down(self) -> None:
        # Option/Alt+Down: browse without loading the preview.
        self._set_scan(True)
        super().action_cursor_down()

    def action_scan_cursor_up(self) -> None:
        self._set_scan(True)
        super().action_cursor_up()

    def _is_selectable_when_expanded(self, node: Any) -> bool:
        """Whether an expanded parent should still accept the cursor.

        Default False: results file rows are dead when open, so the cursor
        skips past them rather than parking where Enter does nothing. The
        filters pane is the opposite — every row there *does* something on
        Enter/click, so all its parent kinds stay selectable while expanded:
        ``filter_category`` section headers (Enter/click collapses them),
        ``kind_category`` file-type rows (toggle the whole category) and
        ``filter_value`` tag rows (toggle the tag). Keeping headers selectable
        also stops a click on an *expanded* header from drifting the cursor
        down onto — and toggling — its first child.
        """
        data = node.data if isinstance(node.data, dict) else None
        return bool(data) and data.get("kind") in (
            "filter_value",
            "kind_category",
            "filter_category",
        )

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
            # An expanded parent that is itself selectable stays reachable.
            # Nested tag rows are real, selectable tags that happen to have
            # children; skipping them would make Enter unable to ever toggle
            # a tag whose subtree is open.
            if self._is_selectable_when_expanded(node):
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

    # ── warmth on the toggle arrow ───────────────────────────────
    #
    # Navigation cost is bimodal: a jump into a file whose hits are captured
    # is a blit, one into a file that still has to be built can be seconds
    # (see fnd/tui/preview/coverage.py). The arrow says which you are about
    # to get, on the two cells the tree already spends on it — so this costs
    # no label width, which matters in a pane whose name budget is already
    # `width - 2 - 7`.
    #
    # Shape carries the fact that changes a decision — hollow means a jump
    # here will build, filled means it will not. Colour carries the rest:
    # blue for cold (the score column's own accent blue, so it reads as part
    # of the same palette), the theme accent for both warm states. Cold and
    # warm are therefore a change of HUE, not of brightness — a muted-vs-
    # accent version of the same glyph was tried on paper and rejected,
    # because at one cell the two are hard to tell apart.
    #
    # Not on match rows. Coverage warms a file's hits nearest-first, so they
    # are all ready within moments of landing, and those rows already carry a
    # glyph for matches the preview cannot highlight — two unrelated marker
    # systems on one row cost more than they tell you.
    ICON_WARM = "▶ "
    ICON_WARM_EXPANDED = "▼ "
    ICON_BUILDING = "▷ "
    ICON_BUILDING_EXPANDED = "▽ "

    #: Tokyo-night's accent blue, the same step the score column uses for its
    #: middle tier — a cold file is not a problem, so it wears a normal colour.
    COLD_COLOUR = "#7aa2f7"

    def render_label(self, node: TreeNode[Any], base_style: Style, style: Style) -> Text:
        """The stock label, with the toggle arrow chosen by warmth.

        Mirrors ``Tree.render_label`` rather than post-processing what it
        returns: the prefix carries the toggle meta that makes clicking the
        arrow expand the node, so it has to be assembled the same way. Every
        icon is two cells wide, like the stock one, so ``get_label_width`` and
        the pane's name budget are unaffected.
        """
        state = self._warm_state_of(node)
        if state is None:
            return super().render_label(node, base_style, style)
        building = state is not WarmState.READY
        if node.is_expanded:
            icon = self.ICON_BUILDING_EXPANDED if building else self.ICON_WARM_EXPANDED
        else:
            icon = self.ICON_BUILDING if building else self.ICON_WARM
        icon_style = base_style + TOGGLE_STYLE
        if state is WarmState.COLD:
            icon_style += Style(color=self.COLD_COLOUR)
        # process_label, not the raw attribute: a label set from a plain str
        # has not been through the tree's own conversion yet.
        node_label = self.process_label(node.label).copy()
        node_label.stylize(style)
        return Text.assemble((icon, icon_style), node_label)

    def _warm_state_of(self, node: TreeNode[Any]) -> WarmState | None:
        """The node's warmth, or None for anything that is not a file row.

        Nodes with no toggle get None too: an arrow that is not drawn cannot
        carry a state, and the tree's own prefix is empty there.
        """
        if not node._allow_expand:
            return None
        data = node.data
        if not isinstance(data, dict) or data.get("kind") != "file":
            return None
        group = data.get("group")
        parent_id = getattr(group, "parent_id", None)
        if parent_id is None:
            return None
        return self.warm_states.get(parent_id)

    def apply_warm_states(self, states: dict[str, WarmState]) -> bool:
        """Adopt ``states`` and repaint only the rows that changed.

        Repainting the whole tree on every capture would strobe the list —
        captures land at roughly ten a second. Diffing means a row is touched
        only when its state actually moves, which for a given file happens
        twice: into WARMING and into READY.

        ``set_label`` with the node's own label is how a row is invalidated:
        it bumps the node's update counter, which is part of the line-cache
        key, so the next paint re-runs ``render_label``. Same mechanism
        ``ResultsView.relabel_file_rows`` already relies on.
        """
        if states == self.warm_states:
            return False
        previous = self.warm_states
        self.warm_states = states
        changed = False
        for node in self.root.children:
            data = node.data
            if not isinstance(data, dict) or data.get("kind") != "file":
                continue
            parent_id = getattr(data.get("group"), "parent_id", None)
            if parent_id is None or previous.get(parent_id) == states.get(parent_id):
                continue
            node.set_label(node.label)
            changed = True
        return changed

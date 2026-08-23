"""Results-tree rendering for the TUI.

``ResultsView`` rebuilds and relabels the results pane from the current
result groups; it owns no state of its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from textual.widgets import Tree

from fnd.tui.match_evidence import evidence_spec_for_pass, has_paintable_match
from fnd.tui.preview.warmth import WarmState
from fnd.tui.results_labels import _format_file_label, _format_hit_label, _styled_parent_label
from fnd.tui.widgets.results_tree import ResultsTree

if TYPE_CHECKING:
    from textual.widgets.tree import TreeNode

    from fnd.query import FileGroup, Hit
    from fnd.tui.app import FNDApp

__all__ = ["ResultsView"]


class ResultsView:
    """Renders the results tree; stateless beyond the app reference."""

    def __init__(self, app: FNDApp) -> None:
        self._app = app

    def title(self) -> str:
        """Border title for the results pane — counts live next to the data
        they describe, not in a global status bar."""
        n_files = len(self._app._search.groups)
        n_sections = sum(len(g.hits) for g in self._app._search.groups)
        if not self._app._search.groups:
            return "Results"
        return f"Results — {n_files} files / {n_sections} sections"

    def refresh(self) -> None:
        """Rebuild the results tree from the current result groups.

        The top result is auto-expanded so its section rows (with their
        ``§ heading`` / ``p.N`` / ``chunk N`` locators) are immediately
        visible — saves a keypress and makes the locator format
        discoverable on first launch.
        """
        # Cancel any debounced preview load from the previous result
        # set — its parent_id may no longer be a hit, and the new
        # cursor placement below will arm a fresh timer.
        self._app._preview.cancel_pending_load()
        tree = self._app.query_one("#results_pane", Tree)
        tree.clear()
        if isinstance(tree, ResultsTree):
            # Every row starts COLD, which after a new query is simply true:
            # the search reset clears the capture store, so nothing is warm.
            #
            # Clearing the map instead was the first attempt and it did NOT
            # work. An unknown row falls through to the stock arrow, and
            # Textual's ICON_NODE is byte-identical to the ready glyph — so
            # "no claim" rendered as "instant jump" for every row until the
            # next poll, at exactly the moment nothing was warm. Anything the
            # tree cannot answer has to fail towards COLD, never towards READY.
            tree.warm_states = dict.fromkeys(
                (g.parent_id for g in self._app._search.groups), WarmState.COLD
            )
        max_score = max((g.top_score for g in self._app._search.groups), default=0.0)
        budget = self.file_label_budget(tree)
        # Rows are never filtered on paintability — see fnd.tui.match_evidence.
        # A row the preview can't highlight is marked, not withheld.
        strict = self._app._effective_evidence_spec
        painting = self._app._effective_match_spec
        unlocatable = 0
        for i, g in enumerate(self._app._search.groups):
            file_node = tree.root.add(
                _styled_parent_label(
                    _format_file_label(g, max_score=max_score, name_budget=budget)
                ),
                data={"kind": "file", "group": g},
                expand=(i == 0),
            )
            for h in g.hits:
                visible = has_paintable_match(
                    h, evidence_spec_for_pass(h.pass_index, strict=strict, painting=painting)
                )
                unlocatable += not visible
                file_node.add_leaf(
                    _format_hit_label(h, max_score=max_score, match_visible=visible),
                    data={"kind": "section", "hit": h},
                )
        if unlocatable:
            # Counted, not just marked: the regression harness asserts this
            # stays at zero, so a highlighting bug is measurable and not merely
            # visible to whoever happens to look at the glyph.
            self._app._preview.diag_log(
                f"results unlocatable={unlocatable} query={self._app._search.current_query!r}"
            )
        self._app._refresh_status()
        if self._app._search.groups:
            # Don't yank focus out of a sidebar panel the user is driving.
            # Toggling a filter re-runs the search, and stealing focus here
            # threw the cursor onto the results tree mid-toggle, so the next
            # key went to the wrong pane. Focus still follows a query the
            # user submits from the query bar.
            if not self._sidebar_has_focus():
                tree.focus()
            # Park cursor on the first hit so the preview already shows the match.
            top_file = tree.root.children[0]
            if top_file.children:
                tree.cursor_line = 1
            # Dispatch explicitly — NodeHighlighted is suppressed when
            # cursor_line lands on the same index as before.
            top_group = self._app._search.groups[0]
            top_hit = top_group.hits[0] if top_group.hits else None
            self._app._preview.schedule_load(
                top_group.parent_id,
                top_hit.chunk_seq if top_hit else 0,
            )

    _SIDEBAR_TREE_IDS = frozenset({"filters_panel_tree", "collections_panel_tree"})

    def _sidebar_has_focus(self) -> bool:
        """Whether the user is currently driving a sidebar panel."""
        focused = self._app.focused
        return focused is not None and focused.id in self._SIDEBAR_TREE_IDS

    @staticmethod
    def file_label_budget(tree: Tree[Any]) -> int:
        """Char budget for a file row's name: the visible content width
        (border + scrollbar excluded) minus the tree's 2-cell row prefix
        (toggle/guide, measured) and the 7-cell score column. 0 before layout."""
        return max(0, tree.scrollable_content_region.width - 2 - 7)

    def relabel_file_rows(self) -> None:
        """Re-elide file-row labels in place (no tree rebuild, so the cursor
        and preview are untouched) — used on resize when the budget changes."""
        try:
            tree = self._app.query_one("#results_pane", Tree)
        except Exception:
            return
        budget = self.file_label_budget(tree)
        max_score = max((g.top_score for g in self._app._search.groups), default=0.0)
        for node in tree.root.children:
            data = node.data
            if isinstance(data, dict) and data.get("kind") == "file":
                node.set_label(
                    _styled_parent_label(
                        _format_file_label(data["group"], max_score=max_score, name_budget=budget)
                    )
                )

    def refresh_warmth(self) -> bool:
        """Repaint any file row whose readiness changed.

        Polled rather than pushed. Warmth changes from two directions — a
        capture landing, and coverage moving to the next file — and the second
        has no single write to hook, so a notification would have to be
        emitted from several places and kept in step with them.

        Cheap enough to poll: one query-signature and one width resolution,
        then a store lookup per listed hit. The listed hits are already capped
        per file by ``defaults.sections_per_file_max``.
        """
        try:
            tree = self._app.query_one("#results_pane", ResultsTree)
        except Exception:
            return False
        states = self._app._preview.warm_states()
        if states is None:
            # Cannot be answered right now — keep what the rows already show.
            # An EMPTY map is different: it means there are no files, and the
            # rows should be cleared rather than left claiming anything.
            return False
        return tree.apply_warm_states(states)

    def refit_after_resize(self) -> None:
        # Result rows are re-elided from ``ResultsTree.GeometryChanged``, not
        # here: this runs a layout early and would elide against the old width.
        self._app._refresh_status()  # preview title

    @staticmethod
    def target_for_node(node: TreeNode[Any]) -> tuple[FileGroup, Hit] | None:
        data: Any = node.data
        if not isinstance(data, dict):
            return None
        kind = data.get("kind")
        if kind == "section":
            hit: Hit = data["hit"]
            parent = node.parent
            if parent is not None and isinstance(parent.data, dict):
                g: FileGroup = parent.data["group"]
                return g, hit
        elif kind == "file":
            g = data["group"]
            if g.hits:
                return g, g.hits[0]
        return None

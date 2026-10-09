"""Results-tree rendering for the TUI.

``ResultsView`` rebuilds and relabels the results pane from the current
result groups; it owns no state of its own.

Search hits arrive light (see :mod:`fnd.query`): a section row shows its
locator until its hit is materialised. The search worker materialises
``UPFRONT_SECTIONS`` in result order; the rest of an expanded file's rows are
materialised off the loop and relabelled in place. The auto-expanded top file
waits out ``_FILL_DEFER_S`` first, the window the controller keeps clear for
the top result's preview (its prefetch waits the same).
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Sequence
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from textual.widgets import Tree

from fnd.query import materialise_hits
from fnd.tui.match_evidence import evidence_spec_for_pass, has_paintable_match
from fnd.tui.preview.presenter import decode_abandonable
from fnd.tui.preview.warmth import WarmState
from fnd.tui.results_labels import (
    _format_file_label,
    _format_hit_label,
    _styled_parent_label,
    disambiguated_names,
    is_stale,
)
from fnd.tui.widgets.results_tree import ResultsTree

if TYPE_CHECKING:
    from textual.widgets.tree import TreeNode

    from fnd.matching import MatchSpec
    from fnd.query import FileGroup, Hit
    from fnd.tui.app import FNDApp

__all__ = ["UPFRONT_SECTIONS", "ResultsView", "materialise_upfront"]

#: Section rows the search worker materialises before the tree is built: about
#: 4 ms each, so the first screen of rows costs about a quarter of a second.
UPFRONT_SECTIONS: Final = 60
_FILL_DEFER_S: Final = 0.5


def materialise_upfront(
    groups: Sequence[FileGroup], spec: MatchSpec, *, intent: str | None
) -> list[FileGroup]:
    """Materialise section hits in result order until ``UPFRONT_SECTIONS`` are spent."""
    budget = UPFRONT_SECTIONS
    out: list[FileGroup] = []
    for g in groups:
        head = materialise_hits(g.hits[:budget], spec, intent=intent) if budget > 0 else []
        budget -= len(head)
        out.append(dataclasses.replace(g, hits=[*head, *g.hits[len(head) :]]))
    return out


def _count(n: int, mark: str, noun: str) -> str:
    """`1 file`, `2 files`, `50+ files`. A floor is never singular."""
    return f"{n}{mark} {noun}" if n == 1 and not mark else f"{n}{mark} {noun}s"


class ResultsView:
    """Renders the results tree; stateless beyond the app reference."""

    def __init__(self, app: FNDApp) -> None:
        self._app = app
        # Paths whose indexed copy no longer matches the disk. Filled by
        # `rebuild`; `relabel_rows` reads it on a resize, which can land
        # before any search has run.
        self._stale: set[str] = set()

    def title(self) -> str:
        """Border title for the results pane — counts live next to the data
        they describe, not in a global status bar.

        A collapsed pane says so: two `←` presses shrink it to one row and the
        state persists across launches, so a title identical to an open pane's
        would make the results look simply gone.
        """
        return f"{self._collapsed_marker()}{self._title_text()}"

    def _collapsed_marker(self) -> str:
        return self._app._scope.collapsed_marker("results_pane")

    def _title_text(self) -> str:
        if not self._app._search.idle:
            # The one operation whose work happens entirely off the loop, so
            # every pane could stay byte-identical for its whole duration.
            return "Results: searching…"
        n_files = len(self._app._search.groups)
        n_sections = sum(len(g.hits) for g in self._app._search.groups)
        if not self._app._search.groups:
            # "Results" alone is what an untouched app shows, so a search that
            # matched nothing was indistinguishable from never having run.
            return "Results" if not self._searched() else "Results: nothing matched"
        # `+` marks a floor: at limit 50 over 290 matches, a bare `50 files`
        # would read as a search that really matched 50.
        trace = self._app._search.latest_trace
        files_mark = "+" if trace is not None and trace.files_truncated else ""
        sections_mark = "+" if trace is not None and trace.sections_truncated else ""
        return (
            f"Results: {_count(n_files, files_mark, 'file')}"
            f" / {_count(n_sections, sections_mark, 'section')}"
        )

    def _searched(self) -> bool:
        return bool((self._app._search.current_query or "").strip())

    def empty_state(self) -> str:
        """What to say when a query matched nothing.

        Names the query, then the reason it is most often not the query: a
        filter set in an earlier session narrows every search silently, and
        the pane's own instruction to press Enter is what a user sees instead.
        """
        query = (self._app._search.current_query or "").strip()
        lines = [f"No results for {query!r}."]
        scope = self._app._scope
        # Nothing ticked searches nothing, and the message explained the tag
        # filters while saying nothing about the emptier reason above them.
        if not scope.collections and not scope.active_sources:
            lines.append("No collections are in scope: tick one in the Collections panel.")
        elif scope.present_kinds_for_scope() == set():
            # An index holding nothing reads exactly like a query matching
            # nothing, and only one of those is about the query. `None` is
            # "could not tell", which is not the same and stays silent.
            lines.append("Nothing is indexed in this scope yet: run Update index from the menu.")
        n_filters = scope.active_filter_count if scope.has_active_filters else 0
        if n_filters:
            # A place, not a keystroke: focus is in the query bar when this
            # paints, so a named key would type itself into the query.
            one = n_filters == 1
            noun = "filter is" if one else "filters are"
            lines.append(
                f"{n_filters} {noun} narrowing this; "
                f"clear {'it' if one else 'them'} in the Filters panel."
            )
        return "\n\n".join(lines)

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
        budget = self.label_budget(tree)
        # Two files can share a basename; the row is the only thing the user
        # has to tell them apart.
        names = disambiguated_names([g.path for g in self._app._search.groups])
        marks = self._app._scope.collection_marks
        self._app._scope.sync_legend(marks)
        # Statted once per rebuild, not per repaint: a resize must not re-walk
        # the disk. The preview renders the INDEX's copy, so a row whose file
        # has gone or moved on needs to say so before the user trusts it.
        self._stale = {g.path for g in self._app._search.groups if is_stale(g)}
        # Rows are never filtered on paintability — see fnd.tui.match_evidence.
        # A row the preview can't highlight is marked, not withheld.
        strict = self._app._effective_evidence_spec
        painting = self._app._effective_match_spec
        unlocatable = 0
        for i, g in enumerate(self._app._search.groups):
            file_node = tree.root.add(
                _styled_parent_label(
                    _format_file_label(
                        g,
                        max_score=max_score,
                        name_budget=budget,
                        display_name=names.get(g.path, ""),
                        stale=g.path in self._stale,
                        mark=marks.file_mark(g.memberships),
                    )
                ),
                data={"kind": "file", "group": g},
                expand=(i == 0),
            )
            for h in g.hits:
                visible = self._match_visible(h, strict, painting)
                unlocatable += not visible
                file_node.add_leaf(
                    _format_hit_label(
                        h, max_score=max_score, match_visible=visible, body_budget=budget
                    ),
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
        for file_node in tree.root.children:
            if file_node.is_expanded:
                self._materialise_rows(file_node, defer=_FILL_DEFER_S)
        if not self._app._search.groups and self._searched():
            self._app._preview.show_pane_message(self.empty_state())
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

    _SIDEBAR_TREE_IDS = frozenset({"outline_pane", "filters_panel_tree", "collections_panel_tree"})

    def _sidebar_has_focus(self) -> bool:
        """Whether the user is currently driving a sidebar panel."""
        focused = self._app.focused
        return focused is not None and focused.id in self._SIDEBAR_TREE_IDS

    @staticmethod
    def label_budget(tree: Tree[Any]) -> int:
        """Char budget for a row's text: the visible content width
        (border + scrollbar excluded) minus the tree's 2-cell row prefix
        (toggle/guide, measured) and the 7-cell score column. 0 before layout."""
        return max(0, tree.scrollable_content_region.width - 2 - 7)

    def relabel_rows(self, *, marks_only: bool = False) -> None:
        """Relabel rows in place, keeping cursor and preview: all on resize, file rows only for marks."""
        try:
            tree = self._app.query_one("#results_pane", Tree)
        except Exception:
            return
        budget = self.label_budget(tree)
        max_score = max((g.top_score for g in self._app._search.groups), default=0.0)
        names = disambiguated_names([g.path for g in self._app._search.groups])
        marks = self._app._scope.collection_marks
        for node in tree.root.children:
            data = node.data
            if isinstance(data, dict) and data.get("kind") == "file":
                group = data["group"]
                node.set_label(
                    _styled_parent_label(
                        _format_file_label(
                            group,
                            max_score=max_score,
                            name_budget=budget,
                            display_name=names.get(group.path, ""),
                            stale=group.path in self._stale,
                            mark=marks.file_mark(group.memberships),
                        )
                    )
                )
                if marks_only:
                    # Marks sit on file rows only; rechecking match rows measured 43 to 171 ms on 50 files.
                    continue
                strict = self._app._effective_evidence_spec
                painting = self._app._effective_match_spec
                for leaf in node.children:
                    leaf_data = leaf.data
                    if not isinstance(leaf_data, dict) or leaf_data.get("kind") != "section":
                        continue
                    hit = leaf_data["hit"]
                    leaf.set_label(
                        _format_hit_label(
                            hit,
                            max_score=max_score,
                            match_visible=self._match_visible(hit, strict, painting),
                            body_budget=budget,
                        )
                    )

    @staticmethod
    def _match_visible(hit: Hit, strict: MatchSpec, painting: MatchSpec) -> bool:
        """Whether the preview can paint ``hit``'s match; a light hit has not been
        checked yet, so it carries no warning until it is materialised."""
        if not hit.materialised:
            return True
        return has_paintable_match(
            hit, evidence_spec_for_pass(hit.pass_index, strict=strict, painting=painting)
        )

    def on_file_expanded(self, node: TreeNode[Any]) -> None:
        """An expanded file's light rows are materialised off the loop."""
        self._materialise_rows(node)

    def _materialise_rows(self, file_node: TreeNode[Any], *, defer: float = 0.0) -> None:
        data = file_node.data
        if not isinstance(data, dict) or data.get("kind") != "file":
            return
        group: FileGroup = data["group"]
        light = [h for h in group.hits if not h.materialised]
        if light:
            self._app.run_worker(
                self._fill_rows(file_node, light, defer),
                group="materialise_rows",
                exit_on_error=False,
            )

    async def _fill_rows(self, file_node: TreeNode[Any], light: list[Hit], defer: float) -> None:
        search = self._app._search
        groups = search.groups
        if defer:
            await asyncio.sleep(defer)
            if search.groups is not groups:
                return
        done = await decode_abandonable(
            partial(materialise_hits, light, search.match_spec, intent=search.intent)
        )
        self._apply_materialised(groups, file_node, done)

    def _apply_materialised(
        self, groups: list[FileGroup], file_node: TreeNode[Any], done: list[Hit]
    ) -> None:
        """Swap materialised hits into the group and relabel their rows, unless a
        newer search has replaced the results since the work was scheduled."""
        if self._app._search.groups is not groups:
            return
        data = file_node.data
        if not isinstance(data, dict):
            return
        by_seq = {h.chunk_seq: h for h in done}
        group: FileGroup = data["group"]
        group.hits[:] = [by_seq.get(h.chunk_seq, h) for h in group.hits]
        try:
            tree = self._app.query_one("#results_pane", Tree)
        except Exception:
            return
        max_score = max((g.top_score for g in groups), default=0.0)
        budget = self.label_budget(tree)
        strict = self._app._effective_evidence_spec
        painting = self._app._effective_match_spec
        unlocatable = 0
        for leaf in file_node.children:
            leaf_data = leaf.data
            if not isinstance(leaf_data, dict) or leaf_data.get("kind") != "section":
                continue
            hit = by_seq.get(leaf_data["hit"].chunk_seq)
            if hit is None or leaf_data["hit"].materialised:
                continue
            leaf_data["hit"] = hit
            visible = self._match_visible(hit, strict, painting)
            unlocatable += not visible
            leaf.set_label(
                _format_hit_label(
                    hit, max_score=max_score, match_visible=visible, body_budget=budget
                )
            )
        if unlocatable:
            self._app._preview.diag_log(
                f"results unlocatable={unlocatable} query={self._app._search.current_query!r}"
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
        # The preview refits its own labels inside the reflow (`MatchAwareScroll.fit_edges`).
        self._app._refresh_results_title()
        self._app._refresh_footer_hints()

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

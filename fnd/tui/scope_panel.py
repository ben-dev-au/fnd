"""Search scope and the sidebar panels that drive it.

``ScopeController`` owns which collections / sources / filters are in
scope, the sidebar panel layout state, and their persistence. The app
delegates the Collections / Filters tree events here; search code reads
the scope back through the app's accessors.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from textual.widgets import Tree

from fnd.tui.results_labels import _styled_parent_label

if TYPE_CHECKING:
    from fnd.tui.app import FNDApp

__all__ = ["ScopeController"]

# Phase F filters: panel layout. ``kinds`` is multi-select (each value
# toggles independently); ``date`` is a radio (single-select; selecting
# a new value replaces the previous). The presentation labels live next
# to the values so the panel renders without further lookup tables.
_FILTER_KINDS: tuple[str, ...] = ("pdf", "docx", "pptx", "md", "txt")
_FILTER_DATES: tuple[str, ...] = ("any", "today", "week", "month", "year")


class ScopeController:
    """Owns scope state (collections / sources / filters), the sidebar
    panel layout, and their persistence to the UI-state file."""

    def __init__(self, app: FNDApp, *, collection: str | None) -> None:
        self._app = app
        # Sidebar panel state — always loaded from disk so user-tuned
        # collapse / expand state survives the next launch, even when
        # ``--collection`` is passed. The CLI flag overrides search
        # *scope* (which collections / sources are active), NOT the
        # *panel layout* (which sidebar containers are collapsed-to-
        # header, which collection rows are expanded). Earlier versions
        # zeroed every persisted set on the ``--collection`` branch and
        # silently dropped the user's panel layout after a single launch
        # with a flag.
        from fnd.state import load as _load_state

        saved = _load_state()
        self.collapsed_panels: set[str] = set(saved.collapsed_panels)
        self.expanded_collections: set[str] = set(saved.expanded_collections)
        # Prune unknown branch names so a renamed branch doesn't get
        # stuck "expanded" forever.
        self.expanded_filter_branches: set[str] = {
            b for b in saved.expanded_filter_branches if b in ("kinds", "date")
        }
        # Scope (collections / sources / filters) — override when
        # ``--collection`` was passed, otherwise restore the persisted
        # scope so the TUI starts where the user left it.
        if collection:
            self.collections: list[str] = [collection]
            self.active_sources: list[str] = []
            self.filter_kinds: list[str] = []
            self.filter_date: str = "any"
        else:
            self.collections = list(saved.collections)
            self.active_sources = list(saved.sources)
            self.filter_kinds = list(saved.filter_kinds)
            self.filter_date = saved.filter_date or "any"
        # Repair a desynced persisted scope: a collection in ``collections``
        # renders ● (whole collection in scope), so every one of its sources
        # must be active. Older builds stripped a shared source when a sibling
        # collection toggled off, leaving the ● collection silently narrowed.
        # The legacy ``sources = []`` shape is left alone (it means "no
        # per-source narrowing", not "partial").
        if self.active_sources:
            for _name in self.collections:
                for _sid in self.collection_source_ids(_name):
                    if _sid not in self.active_sources:
                        self.active_sources.append(_sid)

    def persist(self) -> None:
        """Save the current scope + panel state to disk so the next
        launch starts where the user left off."""
        from fnd.state import UiState, save

        save(
            UiState(
                collections=list(self.collections),
                sources=list(self.active_sources),
                collapsed_panels=sorted(self.collapsed_panels),
                expanded_collections=sorted(self.expanded_collections),
                expanded_filter_branches=sorted(self.expanded_filter_branches),
                filter_kinds=list(self.filter_kinds),
                filter_date=self.filter_date,
            )
        )

    # ── Collections panel (UX-D) ─────────────────────────────────

    def collection_source_ids(self, name: str) -> list[str]:
        """Resolved source IDs for a collection, in declaration order."""
        cfg = self._app._config
        if cfg is None:
            return []
        col = cfg.collections.get(name)
        if col is None:
            return []
        return [str(Path(str(s.path)).expanduser().resolve()) for s in col.sources]

    def collection_marker(self, name: str) -> str:
        """Tri-state marker for the collection row: full / partial / empty.

        ``collections`` membership is the primary "whole collection in
        scope" signal — it's set by the CLI ``--collection`` flag,
        persisted scope, and the UI toggle handler — and reads as ●
        full. The per-source ``active_sources`` set carries the
        finer-grained on/off bits for individual rows and produces the
        ◐ partial state when only some sources are active.

        The toggle handler keeps these in sync (it removes the parent
        from ``collections`` when a single source is turned off, and
        re-adds it when every sibling is back on), so the only paths
        that land in "collection in ``collections`` but no sources in
        ``active_sources``" are the CLI flag and the legacy persisted
        scope — both of which the user explicitly wants displayed as ●.
        """
        if name in self.collections:
            return "●"
        source_ids = self.collection_source_ids(name)
        if not source_ids:
            return "○"
        active_sources = set(self.active_sources)
        n_active = sum(1 for sid in source_ids if sid in active_sources)
        if n_active == 0:
            return "○"
        if n_active == len(source_ids):
            return "●"
        return "◐"

    def refresh_collections_panel(self) -> None:
        """Repopulate the lazygit-style collections panel from the loaded
        Config, marking active collections AND active sources within
        them."""
        try:
            tree = self._app.query_one("#collections_panel_tree", Tree)
        except Exception:
            return
        cfg = self._app._config
        if cfg is None:
            from fnd.config import load as load_config

            try:
                cfg = load_config()
            except Exception:
                cfg = None
        names = sorted(cfg.collections.keys()) if cfg else []
        active_sources = set(self.active_sources)
        # Drop persisted expand entries for collections that no longer
        # exist so the saved set stays bounded over time.
        self.expanded_collections &= set(names)
        tree.show_root = False
        tree.clear()
        active_source_count = 0
        total_source_count = 0
        n_full_collections = 0
        for name in names:
            col = cfg.collections[name] if cfg else None
            marker = self.collection_marker(name)
            if marker == "●":
                n_full_collections += 1
            n_sources = len(col.sources) if col else 0
            total_source_count += n_sources
            label = f"{marker}  {name}  ({n_sources} source{'s' if n_sources != 1 else ''})"
            node = tree.root.add(
                _styled_parent_label(label),
                data={"kind": "collection", "name": name},
                expand=name in self.expanded_collections,
            )
            if col:
                # When the whole collection is in scope (CLI flag,
                # persisted scope, or "all sources on" toggle), each
                # source is implicitly active — the per-source toggle
                # only fills in granular off-bits within an explicitly
                # full collection.
                collection_full = name in self.collections
                for i, s in enumerate(col.sources):
                    source_id = str(Path(str(s.path)).expanduser().resolve())
                    src_active = collection_full or source_id in active_sources
                    if src_active:
                        active_source_count += 1
                    src_marker = "●" if src_active else "○"
                    short = Path(str(s.path)).name or str(s.path)
                    src_label = f"{src_marker}  {i + 1}. {short}"
                    node.add_leaf(
                        src_label,
                        data={
                            "kind": "source",
                            "collection": name,
                            "source_id": source_id,
                        },
                    )
        title = f"Collections · {n_full_collections}/{len(names)} active"
        if total_source_count and active_source_count:
            title += f", {active_source_count}/{total_source_count} sources"
        tree.border_title = title

    # ── Filters panel (UX-F) ──────────────────────────────────────

    def refresh_filters_panel(self) -> None:
        """Repopulate the Filters panel.

        Two top-level branches: ``File type`` (multi-select) and
        ``Date`` (radio). Each value row carries enough data on its
        node to round-trip back to ``on_filters_selected``
        without re-parsing labels.
        """
        try:
            tree = self._app.query_one("#filters_panel_tree", Tree)
        except Exception:
            return
        # Branch expand state lives in ``expanded_filter_branches`` and
        # is persisted across runs. Re-sync it from the live tree before
        # clearing so a NodeExpanded that came in between refreshes isn't
        # lost. (Pruning to known branches happens in __init__.)
        for branch in tree.root.children:
            data = branch.data if isinstance(branch.data, dict) else {}
            cat = data.get("category")
            if isinstance(cat, str) and cat in ("kinds", "date"):
                if branch.is_expanded:
                    self.expanded_filter_branches.add(cat)
                else:
                    self.expanded_filter_branches.discard(cat)
        tree.show_root = False
        tree.clear()

        active_kinds = set(self.filter_kinds)
        kind_summary = f"{len(active_kinds)} of {len(_FILTER_KINDS)}" if active_kinds else "any"
        kind_node = tree.root.add(
            _styled_parent_label(f"File type        ({kind_summary})"),
            data={"kind": "filter_category", "category": "kinds"},
            expand="kinds" in self.expanded_filter_branches,
        )
        for k in _FILTER_KINDS:
            marker = "●" if k in active_kinds else "○"
            kind_node.add_leaf(
                f"{marker}  {k}",
                data={"kind": "filter_value", "category": "kinds", "value": k},
            )

        date_summary = self.filter_date or "any"
        date_node = tree.root.add(
            _styled_parent_label(f"Modified         ({date_summary})"),
            data={"kind": "filter_category", "category": "date"},
            expand="date" in self.expanded_filter_branches,
        )
        for d in _FILTER_DATES:
            marker = "●" if d == self.filter_date else "○"
            date_node.add_leaf(
                f"{marker}  {d}",
                data={"kind": "filter_value", "category": "date", "value": d},
            )

        # Header tracks whether anything is filtering; the dim default
        # keeps the panel quiet when no filters are active.
        active_bits: list[str] = []
        if active_kinds:
            active_bits.append(f"{len(active_kinds)} kind{'s' if len(active_kinds) != 1 else ''}")
        if self.filter_date and self.filter_date != "any":
            active_bits.append(self.filter_date)
        title = "Filters" if not active_bits else f"Filters — {', '.join(active_bits)}"
        tree.border_title = title

    def on_filters_selected(self, ev: Tree.NodeSelected[dict[str, object]]) -> None:
        """Enter on a filter value toggles it.

        - File type: each value toggles independently (multi-select).
        - Date: selecting a value replaces the previous (radio); picking
          ``any`` clears the filter.
        - Selecting a category row is a no-op; expand/collapse is the
          tree's native behaviour for those.
        """
        data = ev.node.data or {}
        kind = data.get("kind")
        if kind != "filter_value":
            return
        category = str(data.get("category") or "")
        value = str(data.get("value") or "")
        if not category or not value:
            return
        if category == "kinds":
            if value in self.filter_kinds:
                self.filter_kinds.remove(value)
            else:
                self.filter_kinds.append(value)
        elif category == "date":
            self.filter_date = value
        else:
            return
        self.refresh_filters_panel()
        self._app._refresh_status()
        self.persist()
        if self._app._current_query:
            self._app._run_query(self._app._current_query)

    def on_collections_selected(self, ev: Tree.NodeSelected[dict[str, object]]) -> None:
        """Enter on a collection node toggles the whole collection's
        scope (all sources at once); Enter on a single source row
        toggles that source independently. Source toggles bubble up so
        the parent collection marker reads ●/◐/○ — full / partial /
        empty — depending on how many of its sources are now active.

        Both toggle paths read "currently on" from BOTH state signals
        (``collections`` membership + per-source ``active_sources``)
        so the visible marker drives the toggle direction, even from
        the legacy / CLI-flag entry case where ``collections`` has the
        collection but ``active_sources`` hasn't been populated yet.
        """
        data = ev.node.data or {}
        kind = data.get("kind")
        if kind == "collection":
            name = str(data.get("name") or "")
            if not name:
                return
            source_ids = self.collection_source_ids(name)
            # ``name in collections`` means "whole collection in scope"
            # — either the user just toggled it via the UI (which also
            # filled ``active_sources``) or the scope arrived from
            # ``--collection`` / legacy persisted state (no per-source
            # bits). Either way the marker reads ● and Enter should
            # turn it off.
            currently_full = name in self.collections or (
                bool(source_ids) and all(sid in self.active_sources for sid in source_ids)
            )
            if currently_full:
                if name in self.collections:
                    self.collections.remove(name)
                if source_ids:
                    # A source shared with a still-active collection stays
                    # on — only drop ids no remaining collection claims.
                    still_claimed = {
                        sid
                        for other in self.collections
                        for sid in self.collection_source_ids(other)
                    }
                    keep = (set(self.active_sources) - set(source_ids)) | (
                        set(self.active_sources) & still_claimed
                    )
                    # Preserve the user's relative ordering of the kept
                    # sources (set difference loses it).
                    self.active_sources = [s for s in self.active_sources if s in keep]
            else:
                if name not in self.collections:
                    self.collections.append(name)
                for sid in source_ids:
                    if sid not in self.active_sources:
                        self.active_sources.append(sid)
        elif kind == "source":
            source_id = str(data.get("source_id") or "")
            if not source_id:
                return
            parent_name = str(data.get("collection") or "")
            sibling_ids = self.collection_source_ids(parent_name) if parent_name else []
            # Normalise the "collections-only" entry case (CLI flag,
            # legacy persisted scope) before deciding the toggle: a
            # source row reads ● when the parent collection is in
            # ``collections``, so flesh out ``active_sources`` to
            # match before flipping a single bit. The very next branch
            # will pop the toggled source back off, leaving every
            # untouched sibling in ``active_sources`` — the partial
            # state the user expected to land in.
            if parent_name and parent_name in self.collections and sibling_ids:
                for sid in sibling_ids:
                    if sid not in self.active_sources:
                        self.active_sources.append(sid)
            if source_id in self.active_sources:
                self.active_sources.remove(source_id)
                # Source went off — the parent collection can no longer
                # be "fully on" by the per-source rule. Drop it from
                # ``collections`` so the search scope reflects what
                # the user sees (partial / empty marker, not a full
                # collection filter).
                if parent_name and parent_name in self.collections:
                    self.collections.remove(parent_name)
            else:
                self.active_sources.append(source_id)
                # Source went on — if that was the last off source in
                # its collection, the collection is now fully on.
                if (
                    parent_name
                    and sibling_ids
                    and all(sid in self.active_sources for sid in sibling_ids)
                    and parent_name not in self.collections
                ):
                    self.collections.append(parent_name)
        else:
            return
        self._app._ranking_profile = self._app._resolve_profile()
        # In-place marker swap on the toggled node (+ siblings whose
        # markers depend on the same source state) instead of
        # ``refresh_collections_panel()``, which calls ``tree.clear()``
        # and resets the cursor to the root every time the user
        # toggles.
        self._update_collections_panel_node(ev.node)
        self._refresh_collections_panel_title()
        self._app._refresh_status()
        self.persist()
        # Don't auto-rerun the active query: the user may be batch-
        # toggling several collections, and each rerun would shift focus
        # to the results pane (via _refresh_results_tree.focus()) and
        # interrupt the run. Drop the now-stale results so it's obvious
        # the next Enter in the query bar re-runs against the new scope;
        # keep _current_query so the user's last query is recallable in
        # the input.
        if self._app._current_query and self._app._groups:
            self._app._clear_query_results()

    def _update_collections_panel_node(self, node: Any) -> None:
        """Swap the marker on a toggled node + cascade to dependent rows.

        Preserves cursor (no ``tree.clear()`` involved). When a
        collection row is toggled, every source child marker is
        repainted too. When a source row is toggled, the parent
        collection's marker is recomputed so its tri-state (●/◐/○)
        reads the new source state.
        """
        data = node.data if isinstance(node.data, dict) else {}
        kind = data.get("kind")
        if kind == "collection":
            name = str(data.get("name") or "")
            if not name:
                return
            self._repaint_collection_node(node, name)
            for child in node.children:
                self._repaint_source_node(child)
            return
        if kind == "source":
            self._repaint_source_node(node)
            parent = node.parent
            if parent is None:
                return
            parent_data = parent.data if isinstance(parent.data, dict) else {}
            parent_name = str(parent_data.get("name") or "")
            if parent_name:
                self._repaint_collection_node(parent, parent_name)

    def _repaint_collection_node(self, node: Any, name: str) -> None:
        cfg = self._app._config
        col = cfg.collections.get(name) if cfg else None
        n_sources = len(col.sources) if col else 0
        marker = self.collection_marker(name)
        label = f"{marker}  {name}  ({n_sources} source{'s' if n_sources != 1 else ''})"
        node.set_label(_styled_parent_label(label))

    def _repaint_source_node(self, node: Any) -> None:
        data = node.data if isinstance(node.data, dict) else {}
        source_id = str(data.get("source_id") or "")
        if not source_id:
            return
        parent_name = str(data.get("collection") or "")
        # Mirror the rule used in ``refresh_collections_panel``: when
        # the parent collection is in ``collections`` (CLI / persisted /
        # toggled-on whole-collection), every child source reads as ●
        # even if ``active_sources`` is empty.
        collection_full = bool(parent_name) and parent_name in self.collections
        src_marker = "●" if collection_full or source_id in self.active_sources else "○"
        current_label = str(node.label)
        # The source label is "<marker>  <i>. <short>" — preserve the
        # ordinal and basename, just swap the marker glyph.
        if len(current_label) > 1 and current_label[0] in ("●", "○"):
            node.set_label(src_marker + current_label[1:])
        else:
            node.set_label(current_label)

    def _refresh_collections_panel_title(self) -> None:
        """Recompute the panel's border-title counts after a toggle.

        Pulled out so toggle handlers can update the counts without
        going through the cursor-resetting tree rebuild. The "active"
        collection count tracks rows that paint as ``●`` (full) — the
        same per-source rule the row marker uses — so the title and
        the row glyphs always agree.
        """
        try:
            tree = self._app.query_one("#collections_panel_tree", Tree)
        except Exception:
            return
        cfg = self._app._config
        names = sorted(cfg.collections.keys()) if cfg else []
        active_sources = set(self.active_sources)
        total_source_count = sum(len(cfg.collections[n].sources) for n in names if cfg)
        active_source_count = 0
        n_full_collections = 0
        for n in names:
            if self.collection_marker(n) == "●":
                n_full_collections += 1
            col = cfg.collections[n] if cfg else None
            if not col:
                continue
            for s in col.sources:
                source_id = str(Path(str(s.path)).expanduser().resolve())
                if source_id in active_sources:
                    active_source_count += 1
        title = f"Collections · {n_full_collections}/{len(names)} active"
        if total_source_count and active_source_count:
            title += f", {active_source_count}/{total_source_count} sources"
        tree.border_title = title

    def on_collection_branch_expanded(self, ev: Tree.NodeExpanded[dict[str, object]]) -> None:
        data = ev.node.data if isinstance(ev.node.data, dict) else {}
        if data.get("kind") != "collection":
            return
        name = str(data.get("name") or "")
        if name and name not in self.expanded_collections:
            self.expanded_collections.add(name)
            self.persist()

    def on_collection_branch_collapsed(self, ev: Tree.NodeCollapsed[dict[str, object]]) -> None:
        data = ev.node.data if isinstance(ev.node.data, dict) else {}
        if data.get("kind") != "collection":
            return
        name = str(data.get("name") or "")
        if name and name in self.expanded_collections:
            self.expanded_collections.discard(name)
            self.persist()

    def on_filter_branch_expanded(self, ev: Tree.NodeExpanded[dict[str, object]]) -> None:
        data = ev.node.data if isinstance(ev.node.data, dict) else {}
        if data.get("kind") != "filter_category":
            return
        cat = str(data.get("category") or "")
        if cat in ("kinds", "date") and cat not in self.expanded_filter_branches:
            self.expanded_filter_branches.add(cat)
            self.persist()

    def on_filter_branch_collapsed(self, ev: Tree.NodeCollapsed[dict[str, object]]) -> None:
        data = ev.node.data if isinstance(ev.node.data, dict) else {}
        if data.get("kind") != "filter_category":
            return
        cat = str(data.get("category") or "")
        if cat in self.expanded_filter_branches:
            self.expanded_filter_branches.discard(cat)
            self.persist()

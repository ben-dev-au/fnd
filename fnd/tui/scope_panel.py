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
_FILTER_CREATED: tuple[str, ...] = ("any", "today", "week", "month", "year")


class _FullScope:
    """Sentinel: whole collection in scope, config-relative and
    unenumerated. Distinct from an explicit ``set`` of source ids so a
    full collection scopes via the collection filter (CLI / persisted /
    all-on) without freezing the source list."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "FULL"


FULL = _FullScope()

# A collection's scope state in ``ScopeController.selection`` is either
# the FULL sentinel (whole collection) or an explicit ``set`` of active
# source ids (partial / granular). Absence from the map = out of scope.


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
            b for b in saved.expanded_filter_branches if b in ("kinds", "date", "created")
        }
        # Scope — one provenance-carrying map (``selection``) is the
        # single source of truth; ``collections`` / ``active_sources``
        # are derived views. Override when ``--collection`` was passed,
        # otherwise reconstruct the map from the persisted flat scope.
        if collection:
            # ``--collection`` is one Option string; accept a comma-separated
            # list and keep only names that exist in the config. Without this
            # a value like ``-c "SSD,SSD Exam"`` becomes a single phantom key
            # that no panel row can toggle yet still pins every search.
            self.selection: dict[str, _FullScope | set[str]] = dict.fromkeys(
                self._valid_collection_names(collection), FULL
            )
            self.filter_kinds: list[str] = []
            self.filter_date: str = "any"
            self.filter_created: str = "any"
        else:
            self.selection = self._derive_selection(saved.collections, saved.sources)
            self.filter_kinds = list(saved.filter_kinds)
            self.filter_date = saved.filter_date or "any"
            self.filter_created = saved.filter_created or "any"

    def _valid_collection_names(self, raw: str) -> list[str]:
        """Resolve a ``--collection`` value to real config collection names.

        A whole-string match wins (so a config name that itself contains a
        comma survives); otherwise the value is split on commas. Unknown
        names are dropped. With no config loaded, the raw value is trusted.
        """
        cfg = self._app._config
        known = set(cfg.collections) if cfg else None
        if known is None:
            return [raw]
        if raw in known:
            return [raw]
        return [n for n in (p.strip() for p in raw.split(",")) if n in known]

    def _derive_selection(
        self, full_names: list[str], flat_sources: list[str]
    ) -> dict[str, _FullScope | set[str]]:
        """Rebuild the selection map from the persisted flat scope.

        Full collections become ``FULL`` (config-relative). Each flat
        source id is attributed as a partial claim to every non-full
        collection whose config contains it — the on-disk shape carries
        no provenance, so a shared id is claimed by all owners. The live
        toggle path records exact provenance; only a save/reload of a
        shared-partial scope reconstructs approximately.
        """
        cfg = self._app._config
        # Drop persisted names with no config collection: a corrupted entry
        # (e.g. a comma-joined ``--collection`` value) has no panel row to
        # toggle yet still drives scope, silently pinning every search. Keep
        # all names when the config is unavailable rather than zeroing scope.
        known = set(cfg.collections) if cfg else None
        names = [n for n in full_names if known is None or n in known]
        sel: dict[str, _FullScope | set[str]] = dict.fromkeys(names, FULL)
        if not flat_sources:
            return sel
        flat = set(flat_sources)
        for name in cfg.collections if cfg else []:
            if sel.get(name) is FULL:
                continue
            claimed = {sid for sid in self.collection_source_ids(name) if sid in flat}
            if claimed:
                sel[name] = claimed
        return sel

    @property
    def collections(self) -> list[str]:
        """Collections fully in scope (●) — the search collection-filter
        channel. Derived from the selection map."""
        return [name for name, sel in self.selection.items() if sel is FULL]

    @property
    def active_sources(self) -> list[str]:
        """Flat active source ids for the per-source search filter.
        Only explicit (partial) selections contribute — FULL collections
        scope via the collection channel. Deterministic config order."""
        out: list[str] = []
        seen: set[str] = set()
        for name, sel in self.selection.items():
            if not isinstance(sel, set):
                continue
            for sid in self.collection_source_ids(name):
                if sid in sel and sid not in seen:
                    seen.add(sid)
                    out.append(sid)
        return out

    def _source_active(self, collection: str, source_id: str) -> bool:
        """O(1) check: is this source row active under its collection?
        The single rule shared by markers, repaint, and title counts."""
        sel = self.selection.get(collection)
        if isinstance(sel, set):
            return source_id in sel
        return sel is FULL

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
                filter_created=self.filter_created,
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
        """Tri-state marker for the collection row: full / partial / empty,
        read straight from the selection map. FULL → ●; absent → ○; an
        explicit set → ● (covers every source), ◐ (some), or ○ (none)."""
        sel = self.selection.get(name)
        if not isinstance(sel, set):
            return "●" if sel is FULL else "○"
        source_ids = self.collection_source_ids(name)
        if not source_ids:
            return "○"
        n_active = sum(1 for sid in source_ids if sid in sel)
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
        # Drop persisted expand entries for collections that no longer
        # exist so the saved set stays bounded over time.
        self.expanded_collections &= set(names)
        tree.show_root = False
        tree.clear()
        for name in names:
            col = cfg.collections[name] if cfg else None
            marker = self.collection_marker(name)
            n_sources = len(col.sources) if col else 0
            label = f"{marker}  {name}  ({n_sources} source{'s' if n_sources != 1 else ''})"
            node = tree.root.add(
                _styled_parent_label(label),
                data={"kind": "collection", "name": name},
                expand=name in self.expanded_collections,
            )
            if col:
                for i, s in enumerate(col.sources):
                    source_id = str(Path(str(s.path)).expanduser().resolve())
                    src_marker = "●" if self._source_active(name, source_id) else "○"
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
        tree.border_title = self._panel_title(names)

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
            if isinstance(cat, str) and cat in ("kinds", "date", "created"):
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

        created_summary = self.filter_created or "any"
        created_node = tree.root.add(
            _styled_parent_label(f"Created          ({created_summary})"),
            data={"kind": "filter_category", "category": "created"},
            expand="created" in self.expanded_filter_branches,
        )
        for c in _FILTER_CREATED:
            marker = "●" if c == self.filter_created else "○"
            created_node.add_leaf(
                f"{marker}  {c}",
                data={"kind": "filter_value", "category": "created", "value": c},
            )

        # Header tracks whether anything is filtering; the dim default
        # keeps the panel quiet when no filters are active.
        active_bits: list[str] = []
        if active_kinds:
            active_bits.append(f"{len(active_kinds)} kind{'s' if len(active_kinds) != 1 else ''}")
        if self.filter_date and self.filter_date != "any":
            active_bits.append(self.filter_date)
        if self.filter_created and self.filter_created != "any":
            active_bits.append(f"created {self.filter_created}")
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
        elif category == "created":
            self.filter_created = value
        else:
            return
        self.refresh_filters_panel()
        self._app._refresh_status()
        self.persist()
        if self._app._search.current_query:
            self._app._search.run(self._app._search.current_query)

    def on_collections_selected(self, ev: Tree.NodeSelected[dict[str, object]]) -> None:
        """Enter on a collection node toggles the whole collection's scope
        (all sources at once); Enter on a single source row toggles that
        source independently. Every change mutates the ``selection`` map —
        the single source of truth — so the visible ●/◐/○ marker drives
        the toggle direction and a shared source's per-collection
        provenance is preserved (toggling one owner off can't strip a
        source a sibling still claims)."""
        data = ev.node.data or {}
        kind = data.get("kind")
        if kind == "collection":
            name = str(data.get("name") or "")
            if not name:
                return
            # Marker ● (FULL or every source on) → off; otherwise → FULL.
            if self.collection_marker(name) == "●":
                self.selection.pop(name, None)
            else:
                self.selection[name] = FULL
        elif kind == "source":
            source_id = str(data.get("source_id") or "")
            if not source_id:
                return
            parent_name = str(data.get("collection") or "")
            if not parent_name:
                return
            self._toggle_source(parent_name, source_id)
        else:
            return
        self._app._search.ranking_profile = self._app._search.resolve_profile()
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
        if self._app._search.current_query and self._app._search.groups:
            self._app._search.clear_results()

    def _toggle_source(self, collection: str, source_id: str) -> None:
        """Flip one source's bit within its collection. FULL resolves to
        the explicit set of every sibling (so dropping one yields a
        partial); a set that grows back to cover all siblings promotes to
        FULL; an emptied set removes the collection from scope."""
        sibling_ids = self.collection_source_ids(collection)
        sel = self.selection.get(collection)
        if isinstance(sel, set):
            current = set(sel)
        elif sel is FULL:
            current = set(sibling_ids)
        else:
            current = set()
        if source_id in current:
            current.discard(source_id)
        else:
            current.add(source_id)
        if not current:
            self.selection.pop(collection, None)
        elif sibling_ids and current.issuperset(sibling_ids):
            self.selection[collection] = FULL
        else:
            self.selection[collection] = current

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
        src_marker = "●" if self._source_active(parent_name, source_id) else "○"
        current_label = str(node.label)
        # The source label is "<marker>  <i>. <short>" — preserve the
        # ordinal and basename, just swap the marker glyph.
        if len(current_label) > 1 and current_label[0] in ("●", "○"):
            node.set_label(src_marker + current_label[1:])
        else:
            node.set_label(current_label)

    def _panel_title(self, names: list[str]) -> str:
        """Border-title string from the selection map. Source counts use
        ``_source_active`` — the same rule the row markers use — so the
        title and the row glyphs always agree (the toggle path and the
        full rebuild both call this)."""
        cfg = self._app._config
        n_full = active = total = 0
        for n in names:
            if self.collection_marker(n) == "●":
                n_full += 1
            col = cfg.collections.get(n) if cfg else None
            if not col:
                continue
            for s in col.sources:
                total += 1
                source_id = str(Path(str(s.path)).expanduser().resolve())
                if self._source_active(n, source_id):
                    active += 1
        title = f"Collections · {n_full}/{len(names)} active"
        if total and active:
            title += f", {active}/{total} sources"
        return title

    def _refresh_collections_panel_title(self) -> None:
        """Recompute the panel's border-title after a toggle without the
        cursor-resetting tree rebuild."""
        try:
            tree = self._app.query_one("#collections_panel_tree", Tree)
        except Exception:
            return
        cfg = self._app._config
        names = sorted(cfg.collections.keys()) if cfg else []
        tree.border_title = self._panel_title(names)

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
        if cat in ("kinds", "date", "created") and cat not in self.expanded_filter_branches:
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

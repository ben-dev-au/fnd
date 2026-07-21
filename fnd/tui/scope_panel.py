"""Search scope and the sidebar panels that drive it.

``ScopeController`` owns which collections / sources / filters are in
scope, the sidebar panel layout state, and their persistence. The app
delegates the Collections / Filters tree events here; search code reads
the scope back through the app's accessors.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from textual.widgets import Tree

from fnd.launch_command import LaunchScope, SearchSnapshot
from fnd.tui.results_labels import _styled_action_label, _styled_parent_label

if TYPE_CHECKING:
    from fnd.tui.app import FNDApp

__all__ = ["ScopeController"]

# Phase F filters: panel layout. ``kinds`` is multi-select (each value
# toggles independently); ``date`` is a radio (single-select; selecting
# a new value replaces the previous). The presentation labels live next
# to the values so the panel renders without further lookup tables.
_FILTER_KINDS: tuple[str, ...] = ("pdf", "docx", "pptx", "md", "txt")
# No "any" row: an unselected filter IS "any". Enter on a value toggles it
# (select, or deselect back to "any" if already selected), consistent with
# the File-type and Tags rows rather than making the user pick an "any" row.
_FILTER_DATES: tuple[str, ...] = ("today", "week", "month", "year")
_FILTER_CREATED: tuple[str, ...] = ("today", "week", "month", "year")
# Provider ids are config keys; these are their pane labels.
_TAG_SOURCE_LABELS: dict[str, str] = {"frontmatter": "Frontmatter", "os": "File tags"}
# Width of Textual's branch expand arrow, so leaf markers line up with it.
_LEAF_MARKER_PAD = "  "


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

    def __init__(
        self, app: FNDApp, *, collection: str | None, launch_filters: LaunchScope | None = None
    ) -> None:
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
        # The filters tree is now wrapped in the #filters_pane container, which
        # carries the collapse state the bare tree used to. Migrate any persisted
        # old id so a user who had filters collapsed keeps it collapsed.
        self.collapsed_panels: set[str] = {
            "filters_pane" if p == "filters_panel_tree" else p for p in saved.collapsed_panels
        }
        self.expanded_collections: set[str] = set(saved.expanded_collections)
        # Prune unknown branch names so a renamed branch doesn't get
        # stuck "expanded" forever.
        self.expanded_filter_branches: set[str] = {
            b
            for b in saved.expanded_filter_branches
            if b in ("kinds", "date", "created") or b == "tags" or b.startswith("tags:")
        }
        # Scope — one provenance-carrying map (``selection``) is the single
        # source of truth; ``collections`` / ``active_sources`` are derived
        # views. A launch-time override (``--collection`` and/or the filter
        # flags — a search copied out of the app) takes scope + filters from
        # the flags; otherwise reconstruct them from the persisted flat scope.
        # Panel *layout* always loads from disk, so a flagged launch never
        # discards the user's sidebar state.
        if collection or launch_filters:
            # ``--collection`` is one Option string; accept a comma-separated
            # list and keep only names that exist in the config. Without this
            # a value like ``-c "SSD,SSD Exam"`` becomes a single phantom key
            # that no panel row can toggle yet still pins every search.
            self.selection: dict[str, _FullScope | set[str]] = (
                dict.fromkeys(self._valid_collection_names(collection), FULL) if collection else {}
            )
            self.filter_kinds: list[str] = []
            self.filter_date: str = "any"
            self.filter_created: str = "any"
            self.tag_include: dict[str, set[str]] = {}
            self.tag_exclude: dict[str, set[str]] = {}
            self.tag_match_all: bool = True
            if launch_filters:
                self._seed_filters(launch_filters)
        else:
            self.selection = self._derive_selection(saved.collections, saved.sources)
            self.filter_kinds = list(saved.filter_kinds)
            self.filter_date = saved.filter_date or "any"
            self.filter_created = saved.filter_created or "any"
            self.tag_include = {k: set(v) for k, v in saved.tag_include.items()}
            self.tag_exclude = {k: set(v) for k, v in saved.tag_exclude.items()}
            self.tag_match_all = saved.tag_match_all

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

    def _tag_source_ids(self) -> list[str]:
        """Provider ids for the configured tag sources — the keys tag
        selections are stored under, shared with the CLI."""
        import sys

        from fnd.tags import providers_for

        cfg = self._app._config
        if cfg is None:
            return []
        return [p.id for p in providers_for(sys.platform, cfg.defaults.tag_sources)]

    def _seed_filters(self, launch: LaunchScope) -> None:
        """Apply a launch-time filter override onto the reset filter fields,
        expanding bare tag flags into per-source sets exactly as the CLI
        ``search`` command does (so the two paths agree)."""
        from fnd.tags import source_tag_selection

        self.filter_kinds = list(launch.kinds)
        self.filter_date = launch.modified or "any"
        self.filter_created = launch.created or "any"
        sources = self._tag_source_ids()
        self.tag_include = {
            k: set(v) for k, v in source_tag_selection(launch.tags, sources).items()
        }
        self.tag_exclude = {
            k: set(v) for k, v in source_tag_selection(launch.not_tags, sources).items()
        }
        self.tag_match_all = launch.tag_match_all

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

    def snapshot(self, query: str) -> SearchSnapshot:
        """Project the live scope into the read-only value object the command
        serializer consumes — the one seam between scope state and
        serialization, so neither reaches into the other."""
        partial = tuple(
            name for name, sel in self.selection.items() if isinstance(sel, set) and sel
        )
        return SearchSnapshot(
            query=query,
            full_collections=tuple(self.collections),
            partial_collections=partial,
            filter_kinds=tuple(self.filter_kinds),
            filter_date=self.filter_date,
            filter_created=self.filter_created,
            tag_include={k: frozenset(v) for k, v in self.tag_include.items() if v},
            tag_exclude={k: frozenset(v) for k, v in self.tag_exclude.items() if v},
            tag_match_all=self.tag_match_all,
        )

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
                tag_include={k: sorted(v) for k, v in self.tag_include.items() if v},
                tag_exclude={k: sorted(v) for k, v in self.tag_exclude.items() if v},
                tag_match_all=self.tag_match_all,
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
        # The collections list changed length — reflow the sidebar heights.
        self._app._reflow_sidebar()

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
        # The rebuild below clears the tree, which resets the cursor to the
        # top. Remember which row was selected so a toggle leaves the cursor
        # on the tag the user just pressed rather than throwing them back to
        # the first branch.
        keep = self._row_key(tree.cursor_node.data) if tree.cursor_node is not None else None
        tree.show_root = False
        tree.clear()

        # A one-shot escape hatch, shown only when there's something to clear
        # so it never adds noise to a clean pane.
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

        self._render_tags_branch(tree)

        # Header tracks whether anything is filtering; the dim default
        # keeps the panel quiet when no filters are active.
        active_bits: list[str] = []
        if active_kinds:
            active_bits.append(f"{len(active_kinds)} kind{'s' if len(active_kinds) != 1 else ''}")
        if self.filter_date and self.filter_date != "any":
            active_bits.append(self.filter_date)
        if self.filter_created and self.filter_created != "any":
            active_bits.append(f"created {self.filter_created}")
        n_inc = len(self._distinct_tag_values(self.tag_include))
        n_exc = len(self._distinct_tag_values(self.tag_exclude))
        if n_inc:
            active_bits.append(f"{n_inc} tag{'s' if n_inc != 1 else ''}")
        if n_exc:
            active_bits.append(f"−{n_exc} tag{'s' if n_exc != 1 else ''}")
        title = "Filters" if not active_bits else f"Filters — {', '.join(active_bits)}"
        try:
            self._app.query_one("#filters_pane").border_title = title
        except Exception:
            tree.border_title = title  # pre-mount fallback
        if keep is not None:
            self._restore_cursor(tree, keep)
        self._update_clear_bar()
        # The rebuilt tag list (and the clear bar showing/hiding) changed the
        # filters pane's row demand — reflow the sidebar heights.
        self._app._reflow_sidebar()

    # ── Clear all filters ─────────────────────────────────────────

    def _action_colour(self) -> str:
        """Control rows take the *inactive pane border* colour so they read as
        interactive without competing with the focused-pane accent.

        The border is ``round $primary 50%`` — primary at 50% opacity over the
        app background — so a full-strength ``$primary`` label looks noticeably
        brighter. Reproduce the same blend here. Resolved live so it tracks the
        theme; empty string (plain text) if the app isn't mounted yet."""
        try:
            from textual.color import Color

            variables = self._app.get_css_variables()
            primary = variables.get("primary")
            if not primary:
                return ""
            # Use the border's exact foreground: $primary 50% composited over
            # the pane surface (confirmed #6F6199 on the default theme). Text
            # glyphs and the border's box-drawing glyphs are both thin strokes,
            # so the SAME foreground perceives identically — which is what makes
            # the rows read as the inactive-border colour rather than
            # a fresh, brighter purple.
            base = variables.get("surface") or variables.get("background") or "#000000"
            return Color.parse(base).blend(Color.parse(primary), 0.5).hex
        except Exception:
            return ""

    @staticmethod
    def _distinct_tag_values(by_source: dict[str, set[str]]) -> set[str]:
        """Tag values across all sources, deduped. The search groups tags by
        value — a value selected in several sources is a single OR-ed term
        (see fnd.tag_query._terms) — so every filter count follows that view
        rather than double-counting a value that fans across sources (e.g. a
        copied ``--tag``, which carries no source and seeds into all of them)."""
        values: set[str] = set()
        for vals in by_source.values():
            values |= vals
        return values

    @property
    def active_filter_count(self) -> int:
        """How many individual filter selections are active — the number shown
        on the Clear bar (kinds + date + created + each included/excluded tag)."""
        return (
            len(self.filter_kinds)
            + (1 if self.filter_date not in ("", "any") else 0)
            + (1 if self.filter_created not in ("", "any") else 0)
            + len(self._distinct_tag_values(self.tag_include))
            + len(self._distinct_tag_values(self.tag_exclude))
        )

    @property
    def has_active_filters(self) -> bool:
        """Whether any filter is narrowing results. Excludes collection/source
        scope, which is not a filter, and the tag match mode, which is a mode."""
        return bool(
            self.filter_kinds
            or (self.filter_date and self.filter_date != "any")
            or (self.filter_created and self.filter_created != "any")
            or any(self.tag_include.values())
            or any(self.tag_exclude.values())
        )

    def _update_clear_bar(self) -> None:
        """Show/hide the pinned clear bar docked at the bottom of the filters
        container, so it floats in view whatever the tag list's scroll. Content
        carries the X hint. The bar is a real widget (clickable); X clears from
        anywhere too."""
        from textual.widgets import Static

        try:
            bar = self._app.query_one("#clear_filters_bar", Static)
        except Exception:
            return
        active = self.has_active_filters
        bar.display = active
        if active:
            n = self.active_filter_count
            plural = "" if n == 1 else "s"
            bar.update(f"✕  Clear {n} filter{plural}")

    def clear_filters(self) -> None:
        """Reset every filter to its default and re-run the active query.

        Collections/sources are scope, not filters, so they are left alone —
        clearing them would silently change what corpus is searched. The tag
        match mode returns to its ``all`` default so the pane is fully reset.
        A no-op when nothing is active, so a stray keypress can't thrash search.
        """
        if not self.has_active_filters and self.tag_match_all:
            return
        self.filter_kinds = []
        self.filter_date = "any"
        self.filter_created = "any"
        self.tag_include = {}
        self.tag_exclude = {}
        self.tag_match_all = True
        self.refresh_filters_panel()
        self._app._refresh_status()
        self.persist()
        if self._app._search.current_query:
            self._app._search.run(self._app._search.current_query)

    # ── Tags branch ───────────────────────────────────────────────

    def tag_catalog_for_scope(self) -> dict[str, list[Any]]:
        """Tags present in the active collections, per source.

        Returns empty lists when the index isn't open yet or the aggregation
        fails — the pane must still render.
        """
        from fnd.tag_catalog import tag_catalog

        searcher = getattr(self._app._search, "searcher", None)
        index = getattr(searcher, "_index", None)
        if index is None:
            return {}
        cfg = self._app._config
        sources = list(cfg.defaults.tag_sources) if cfg else None
        try:
            return tag_catalog(
                index,
                collections=self.collections,
                sources=sources,
                query=self._facet_query(index),
            )
        except Exception:
            return {}

    def _facet_query(self, index: Any) -> Any:
        """Tantivy query narrowing the tag catalogue to the active search.

        Deliberately built from the lexical text alone — NOT from the tag
        selection. Facets computed over their own filter make every sibling
        tag vanish the moment one is selected, stranding the user with no way
        to switch without clearing first.

        A cheap parse rather than the ranked pipeline: facets need membership,
        not ordering. Returns None (whole collection scope) when no query is
        active or the text can't be parsed, so the pane stays browsable.
        """
        raw = (self._app._search.current_query or "").strip()
        if not raw:
            return None
        try:
            from fnd.query_plan import QueryPlan
            from fnd.schema import DEFAULT_SEARCH_FIELDS

            lexical = QueryPlan.from_user_text(raw).lexical.strip()
            if not lexical:
                return None
            return index.parse_query(lexical, DEFAULT_SEARCH_FIELDS)
        except Exception:
            return None

    def tag_marker(self, source: str, node: Any) -> str:
        """``●`` included, ``⊘`` excluded, ``◐`` a descendant is selected, ``○`` off.

        Selecting a parent already covers its subtree (ancestors are expanded
        at index time), so ``◐`` only ever means "something below me is
        selected but I am not".
        """
        value = node.value
        if value in self.tag_include.get(source, set()):
            return "●"
        if value in self.tag_exclude.get(source, set()):
            return "⊘"
        below = node.descendant_values() - {value}
        touched = self.tag_include.get(source, set()) | self.tag_exclude.get(source, set())
        return "◐" if below & touched else "○"

    def _add_tag_nodes(
        self,
        parent: Any,
        source: str,
        nodes: list[Any],
        depth: int,
        namespaces: frozenset[str] = frozenset(),
    ) -> None:
        """Render one level of the tag tree.

        ``namespaces`` are values that came from a configured frontmatter KEY
        (``Course``, ``Notes_Type``) rather than a tag the user wrote. They
        name a field, not a tag, so they render as plain headers: no marker,
        not selectable. Nested tag parents like ``project`` in
        ``project/alpha`` stay selectable — that one IS a real tag.
        """
        for node in nodes:
            is_namespace = depth == 0 and node.value in namespaces
            key = f"tags:{source}:{node.value}"
            if is_namespace:
                branch = parent.add(
                    _styled_parent_label(f"{node.label}  ({node.files})"),
                    data={"kind": "filter_category", "category": key},
                    expand=key in self.expanded_filter_branches,
                )
                self._add_tag_nodes(branch, source, node.children, depth + 1, namespaces)
                continue

            marker = self.tag_marker(source, node)
            data = {
                "kind": "filter_value",
                "category": "tags",
                "source": source,
                "value": node.value,
            }
            if node.children:
                branch = parent.add(
                    f"{marker}  {node.label}  ({node.files})",
                    data=data,
                    expand=key in self.expanded_filter_branches,
                )
                self._add_tag_nodes(branch, source, node.children, depth + 1, namespaces)
            else:
                # Textual prefixes branch rows with a 2-cell expand arrow but
                # leaves none on leaves, so a leaf's marker would sit two
                # columns left of its branch siblings'. Pad to line them up.
                parent.add_leaf(
                    f"{_LEAF_MARKER_PAD}{marker}  {node.label}  ({node.files})", data=data
                )

    def _frontmatter_namespaces(self) -> frozenset[str]:
        """Normalised tag values that are really frontmatter FIELD names.

        Mirrors the namespacing fnd.tags applies at index time, so the pane
        can tell ``course`` (a field) from ``project`` (a genuine tag).
        """
        cfg = self._app._config
        if cfg is None:
            return frozenset()
        from fnd.tags import normalise_tag

        return frozenset(
            t for t in (normalise_tag(k) for k in cfg.defaults.tag_frontmatter_keys) if t
        )

    def _render_tags_branch(self, tree: Tree[dict[str, object]]) -> None:
        from fnd.tag_catalog import build_tag_tree

        catalog = self.tag_catalog_for_scope()
        namespaces = self._frontmatter_namespaces()
        n_selected = len(self._distinct_tag_values(self.tag_include)) + len(
            self._distinct_tag_values(self.tag_exclude)
        )
        n_available = sum(len(v) for v in catalog.values())
        summary = f"{n_selected} of {n_available}" if n_available else "none indexed"
        tags_node = tree.root.add(
            _styled_parent_label(f"Tags             ({summary})"),
            data={"kind": "filter_category", "category": "tags"},
            expand="tags" in self.expanded_filter_branches,
        )
        if not n_available:
            return

        mode = "all" if self.tag_match_all else "any"
        tags_node.add_leaf(
            _styled_action_label(f"⇄  Match: {mode}", self._action_colour()),
            data={"kind": "filter_value", "category": "tag_match", "value": "toggle"},
        )
        for source, counts in catalog.items():
            if not counts:
                continue
            branch = tags_node.add(
                _styled_parent_label(f"{_TAG_SOURCE_LABELS.get(source, source)}"),
                data={"kind": "filter_category", "category": f"tags:{source}"},
                expand=f"tags:{source}" in self.expanded_filter_branches,
            )
            self._add_tag_nodes(branch, source, build_tag_tree(counts), 0, namespaces)

    def _cycle_tag(self, source: str, value: str) -> None:
        """``○ off → ● include → ⊘ exclude → off``."""
        inc = self.tag_include.setdefault(source, set())
        exc = self.tag_exclude.setdefault(source, set())
        if value in inc:
            inc.discard(value)
            exc.add(value)
        elif value in exc:
            exc.discard(value)
        else:
            inc.add(value)

    @staticmethod
    def _row_key(data: object) -> tuple[str, ...] | None:
        """Identity of a filters row that survives a rebuild.

        Node objects are discarded by ``tree.clear()``, so the cursor is
        restored by matching this key against the freshly-built rows.
        """
        if not isinstance(data, dict):
            return None
        return (
            str(data.get("kind") or ""),
            str(data.get("category") or ""),
            str(data.get("source") or ""),
            str(data.get("value") or ""),
        )

    def _restore_cursor(self, tree: Tree[dict[str, object]], keep: tuple[str, ...]) -> None:
        """Put the cursor back on the row identified by ``keep``, if it still
        exists. A tag can legitimately vanish (its last file left the result
        set), in which case the cursor stays where the rebuild left it."""
        for line, tree_line in enumerate(tree._tree_lines):
            if self._row_key(tree_line.node.data) == keep:
                tree.cursor_line = line
                # Setting cursor_line alone doesn't re-scroll, so when the clear
                # bar appears above it (shrinking the tree by a row) the restored
                # cursor can sit one row out of view. Scroll it back — DEFERRED
                # to after the refresh, because the bar's show/hide resizes the
                # tree on the next layout pass, after this runs; scrolling now
                # would target the pre-resize height and still clip the row.
                with contextlib.suppress(Exception):
                    self._app.call_after_refresh(tree.scroll_to_line, line, animate=False)
                return

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
            self.filter_date = "any" if self.filter_date == value else value
        elif category == "created":
            self.filter_created = "any" if self.filter_created == value else value
        elif category == "tag_match":
            self.tag_match_all = not self.tag_match_all
        elif category == "tags":
            source = str(data.get("source") or "")
            if not source:
                return
            self._cycle_tag(source, value)
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

    def _branch_key(self, data: dict[str, object]) -> str:
        """Expand-state key for a filters-pane branch, or "" if it has none.

        Nested tag rows are ``filter_value`` nodes (they are selectable tags
        that also happen to have children), so keying on ``filter_category``
        alone would silently drop their expand state.
        """
        kind = data.get("kind")
        if kind == "filter_category":
            return str(data.get("category") or "")
        if kind == "filter_value" and data.get("category") == "tags":
            return f"tags:{data.get('source')}:{data.get('value')}"
        return ""

    def on_filter_branch_expanded(self, ev: Tree.NodeExpanded[dict[str, object]]) -> None:
        data = ev.node.data if isinstance(ev.node.data, dict) else {}
        cat = self._branch_key(data)
        if not cat:
            return
        known = cat in ("kinds", "date", "created") or cat == "tags" or cat.startswith("tags:")
        if known and cat not in self.expanded_filter_branches:
            self.expanded_filter_branches.add(cat)
            self.persist()

    def on_filter_branch_collapsed(self, ev: Tree.NodeCollapsed[dict[str, object]]) -> None:
        data = ev.node.data if isinstance(ev.node.data, dict) else {}
        cat = self._branch_key(data)
        if not cat:
            return
        if cat in self.expanded_filter_branches:
            self.expanded_filter_branches.discard(cat)
            self.persist()

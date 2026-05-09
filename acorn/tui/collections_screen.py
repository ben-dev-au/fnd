"""TUI Collections form (§5.5e-3).

Full-screen Textual ``Screen`` that lists configured collections and lets
the user edit, add, or delete them without leaving the TUI. Saves persist
to ``config.toml`` via :func:`acorn.config.write_collection`, which uses
``tomlkit`` so user-authored comments survive the round-trip.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Input, Static, TextArea, Tree
from textual.widgets.tree import TreeNode

from acorn.config import Config, SourceConfig


def _strip_wrapping_quotes(text: str) -> str:
    """Drop matched surrounding ``'`` or ``"`` from a path-like string.

    Paste-with-quotes is the dominant habit for paths with spaces. The
    path field is always a literal filesystem path; the quote characters
    are never part of a real path. Mismatched / one-sided quotes are
    left intact (probably a bug in the user's input).
    """
    text = text.strip()
    if len(text) >= 2 and text[0] in ('"', "'") and text[-1] == text[0]:
        return text[1:-1].strip()
    return text


class CollectionsScreen(Screen[None]):
    """Top-level Collections form. Pushed from the main app via F3.

    Layout: a single :class:`Tree` widget where collection nodes expand
    into source children — same shape as the search results tree, so
    ``j``/``k`` navigate the whole structure with no per-pane cursor.
    Actions (``e``/``a``/``x``/``d``/``s``) operate on whichever node is
    focused, and resolve a "current collection" from the focused node
    (the source's parent if a source is focused).
    """

    BINDINGS = [  # noqa: RUF012 — Textual class-list pattern
        Binding("escape", "close", "Close", show=True),
        Binding("j,down", "tree_next", "Next", show=False),
        Binding("k,up", "tree_prev", "Prev", show=False),
        Binding("l,right", "tree_expand", "Expand", show=False),
        Binding("h,left", "tree_collapse", "Collapse", show=False),
        Binding("e,enter", "edit_source", "Edit", show=True),
        Binding("a", "add_source", "Add source", show=True),
        Binding("x", "remove_source", "Remove source", show=True),
        Binding("s", "save", "Save", show=True),
        Binding("d", "delete_collection", "Delete", show=True),
        Binding("n", "new_collection", "New", show=True),
    ]

    CSS = """
    CollectionsScreen { background: $surface; }
    #collections_title { dock: top; height: 1; padding: 0 1; background: $panel; color: $accent; text-style: bold; }
    #collections_tree_box { width: 100%; height: 1fr; border: round $primary; padding: 0 1; }
    #collections_tree { height: 1fr; }
    """

    def __init__(self, config: Config, *, config_path: Path) -> None:
        super().__init__()
        self._config = config
        self._config_path = config_path
        # Deep-copy the source list per collection at form open. Compared
        # against the live ``self._config`` on save to decide whether a
        # reindex is needed.
        self._initial: dict[str, list[SourceConfig]] = {
            name: deepcopy(c.sources) for name, c in config.collections.items()
        }

    def compose(self) -> ComposeResult:
        yield Static("Collections", id="collections_title")
        with Vertical(id="collections_tree_box"):
            tree: Tree[dict[str, object]] = Tree("Collections", id="collections_tree")
            tree.show_root = False
            tree.guide_depth = 2
            yield tree
        yield Footer()

    def on_mount(self) -> None:
        self._rebuild_tree()
        tree = self.query_one("#collections_tree", Tree)
        tree.focus()

    # ── Tree state ────────────────────────────────────────────────

    def _rebuild_tree(
        self,
        *,
        focus: tuple[str | None, int | None] | None = None,
    ) -> None:
        """Wipe and rebuild the tree from ``self._config``.

        ``focus`` is an optional ``(collection_name, source_index_or_None)``
        pair — after the rebuild we walk the new nodes and try to land
        the cursor on the matching one. If omitted we preserve the
        currently-focused (collection, source) — which is what every
        action needs after a mutation.
        """
        if focus is None:
            focus = (self._focused_collection_name(), self._focused_source_index())
        tree = self.query_one("#collections_tree", Tree)
        tree.clear()
        for name in sorted(self._config.collections.keys()):
            c = self._config.collections[name]
            count = len(c.sources)
            label = f"{name}  ({count} source{'s' if count != 1 else ''})  [{c.ranking_profile}]"
            collection_node = tree.root.add(
                label, data={"kind": "collection", "name": name}, expand=True
            )
            for i, s in enumerate(c.sources):
                bits = [f"{i + 1}. {s.path}"]
                if s.includes:
                    bits.append(f"includes: {', '.join(s.includes)}")
                if s.excludes:
                    bits.append(f"excludes: {', '.join(s.excludes)}")
                if s.frontmatter_filter:
                    bits.append(f"filter: {s.frontmatter_filter}")
                collection_node.add_leaf(
                    "  ·  ".join(bits),
                    data={"kind": "source", "collection": name, "index": i},
                )
        # Restore focus if possible.
        self._focus_node(*focus)

    def _focus_node(self, collection_name: str | None, source_index: int | None) -> None:
        """Move the tree cursor to the node identified by
        ``(collection_name, source_index)``; falls back to the first
        collection, or the root if the tree is empty.

        Tantivy's :meth:`Tree.select_node` reads ``node._line`` to derive
        the new ``cursor_line`` — but ``_line`` is only populated by
        :meth:`Tree._build`, which Textual schedules lazily on idle.
        Right after a rebuild the values are stale, so we touch
        ``_tree_lines`` first to force a synchronous build before
        selecting.
        """
        tree = self.query_one("#collections_tree", Tree)
        _ = tree._tree_lines
        target: TreeNode[dict[str, object]] | None = None
        if collection_name is not None:
            for c_node in tree.root.children:
                data = c_node.data or {}
                if data.get("kind") == "collection" and data.get("name") == collection_name:
                    if source_index is None or not c_node.children:
                        target = c_node
                        break
                    idx = min(source_index, len(c_node.children) - 1)
                    target = c_node.children[idx]
                    break
        if target is None and tree.root.children:
            target = tree.root.children[0]
        if target is not None:
            # ``move_cursor`` (not ``select_node``) — the latter posts a
            # NodeSelected event whose default handler auto-toggles the
            # node's expand state when ``auto_expand=True``, which collapses
            # nodes we just added with ``expand=True``.
            tree.move_cursor(target)

    def _focused_node(self) -> TreeNode[dict[str, object]] | None:
        try:
            tree = self.query_one("#collections_tree", Tree)
        except Exception:
            return None
        return tree.cursor_node

    def _focused_collection_name(self) -> str | None:
        """Resolve the focused-or-parent collection name from the tree
        cursor. Returns None only when the tree is empty."""
        node = self._focused_node()
        if node is None:
            return None
        data = node.data or {}
        kind = data.get("kind")
        if kind == "collection":
            return str(data.get("name") or "") or None
        if kind == "source":
            return str(data.get("collection") or "") or None
        return None

    def _focused_source_index(self) -> int | None:
        node = self._focused_node()
        if node is None:
            return None
        data = node.data or {}
        if data.get("kind") == "source":
            idx = data.get("index", 0)
            return int(idx) if isinstance(idx, int) else 0
        return None

    # Backwards-compat properties — older tests assert these.

    @property
    def _selected(self) -> str | None:
        return self._focused_collection_name()

    @_selected.setter
    def _selected(self, value: str | None) -> None:
        # Setter is a no-op aside from re-positioning the tree cursor;
        # kept so existing assignment-style mutations don't error during
        # the gradual migration to tree-driven state.
        if value is None:
            return
        self._focus_node(value, None)

    @property
    def _source_cursor(self) -> int:
        idx = self._focused_source_index()
        return idx if idx is not None else 0

    @_source_cursor.setter
    def _source_cursor(self, value: int) -> None:
        self._focus_node(self._focused_collection_name(), value)

    # ── Actions ───────────────────────────────────────────────────

    def action_close(self) -> None:
        self.dismiss(None)

    def action_tree_next(self) -> None:
        tree = self.query_one("#collections_tree", Tree)
        tree.action_cursor_down()

    def action_tree_prev(self) -> None:
        tree = self.query_one("#collections_tree", Tree)
        tree.action_cursor_up()

    def action_tree_expand(self) -> None:
        node = self._focused_node()
        if node is not None and not node.is_expanded:
            node.expand()

    def action_tree_collapse(self) -> None:
        """Lazygit-style smart collapse: a leaf or already-collapsed branch
        backs out one level by collapsing its parent and moving the cursor
        onto it. Expanded branches collapse in place."""
        tree = self.query_one("#collections_tree", Tree)
        node = self._focused_node()
        if node is None:
            return
        if not node.children or not node.is_expanded:
            parent = node.parent
            if parent is None or parent is tree.root:
                return
            parent.collapse()
            tree.move_cursor(parent)
            return
        node.collapse()

    def action_edit_source(self) -> None:
        name = self._focused_collection_name()
        idx = self._focused_source_index()
        if name is None or idx is None:
            # Cursor is on a collection node (or the tree is empty) —
            # editing collection-level metadata isn't in 5.5e-3 scope.
            return
        c = self._config.collections[name]
        s = c.sources[idx]
        screen = SourceEditScreen(
            title=f"{name} / {idx + 1}",
            path=str(s.path),
            includes=list(s.includes),
            excludes=list(s.excludes),
            frontmatter_filter=s.frontmatter_filter,
        )
        self.app.push_screen(
            screen,
            callback=lambda r: self._apply_source_edit(name, idx, r),
        )

    def action_add_source(self) -> None:
        name = self._focused_collection_name()
        if name is None:
            return
        screen = SourceEditScreen(
            title=f"{name} / new",
            path="",
            includes=[],
            excludes=[],
            frontmatter_filter=None,
        )
        self.app.push_screen(screen, callback=lambda r: self._on_new_source_dismissed(name, r))

    def _on_new_source_dismissed(
        self, collection_name: str, result: dict[str, object] | None
    ) -> None:
        if result is None:
            return
        c = self._config.collections[collection_name]
        c.sources.append(
            SourceConfig(
                path=Path(str(result["path"])),
                includes=list(result["includes"]),  # type: ignore[arg-type]
                excludes=list(result["excludes"]),  # type: ignore[arg-type]
                frontmatter_filter=result.get("frontmatter_filter"),  # type: ignore[arg-type]
            )
        )
        new_index = len(c.sources) - 1
        self._rebuild_tree(focus=(collection_name, new_index))

    def action_remove_source(self) -> None:
        name = self._focused_collection_name()
        idx = self._focused_source_index()
        if name is None or idx is None:
            return
        c = self._config.collections[name]
        if not c.sources:
            return
        del c.sources[idx]
        new_idx = min(idx, len(c.sources) - 1) if c.sources else None
        self._rebuild_tree(focus=(name, new_idx))

    def action_save(self) -> None:
        name = self._focused_collection_name()
        if name is None:
            return
        from acorn.config import write_collection
        from acorn.index import build_index_from_config

        c = self._config.collections[name]
        write_collection(
            config_path=self._config_path,
            name=name,
            collection=c,
        )
        # Did anything structural change? If so, reindex synchronously.
        if self._needs_reindex(name):
            self.app.notify(
                f"Reindexing {name}…",
                severity="information",
                timeout=2,
            )
            try:
                n = build_index_from_config(
                    config=c,
                    collection=name,
                    index_dir=self.app._index_dir,  # type: ignore[attr-defined]
                    rebuild=True,
                )
                self.app.notify(
                    f"Indexed {n} chunks for {name}.",
                    severity="information",
                )
            except Exception as e:
                self.app.notify(f"Reindex failed: {e}", severity="error")
        else:
            self.app.notify(f"Saved {name}", severity="information")
        # Refresh snapshot so subsequent saves diff against the new state.
        self._initial[name] = deepcopy(c.sources)

    def _needs_reindex(self, name: str) -> bool:
        prev = self._initial.get(name, [])
        curr = self._config.collections[name].sources
        if len(prev) != len(curr):
            return True
        for a, b in zip(prev, curr, strict=True):
            if (
                a.path != b.path
                or list(a.includes) != list(b.includes)
                or list(a.excludes) != list(b.excludes)
                or a.frontmatter_filter != b.frontmatter_filter
                or a.follow_symlinks != b.follow_symlinks
            ):
                return True
        return False

    def _apply_source_edit(
        self,
        collection_name: str,
        index: int,
        result: dict[str, object] | None,
    ) -> None:
        if result is None:
            return
        c = self._config.collections[collection_name]
        new_source = SourceConfig(
            path=Path(str(result["path"])),
            includes=list(result["includes"]),  # type: ignore[arg-type]
            excludes=list(result["excludes"]),  # type: ignore[arg-type]
            frontmatter_filter=result.get("frontmatter_filter"),  # type: ignore[arg-type]
            follow_symlinks=c.sources[index].follow_symlinks,
        )
        c.sources[index] = new_source
        self._rebuild_tree(focus=(collection_name, index))

    def action_delete_collection(self) -> None:
        name = self._focused_collection_name()
        if name is None:
            return
        screen = _DeleteConfirmScreen(f"Delete collection '{name}' and remove its indexed chunks?")
        self.app.push_screen(screen, callback=lambda r: self._on_delete_confirmed(name, r))

    def _on_delete_confirmed(self, name: str, ok: bool | None) -> None:
        if not ok:
            return
        from acorn.config import delete_collection
        from acorn.index import _ensure_index
        from acorn.schema import F_COLLECTION

        # 1. Remove from on-disk config.
        delete_collection(config_path=self._config_path, name=name)
        # 2. Remove from in-memory Config so the form re-renders without it.
        self._config.collections.pop(name, None)
        self._initial.pop(name, None)
        # 3. Drop chunks from the index.
        try:
            index = _ensure_index(self.app._index_dir)  # type: ignore[attr-defined]
            writer = index.writer(heap_size=50_000_000)
            writer.delete_documents(F_COLLECTION, name)
            writer.commit()
            writer.wait_merging_threads()
        except Exception as e:
            self.app.notify(f"Failed to drop chunks: {e}", severity="error")
        # 4. Move focus to the next collection (or root if empty).
        names = sorted(self._config.collections.keys())
        next_focus = names[0] if names else None
        self._rebuild_tree(focus=(next_focus, None))
        self.app.notify(f"Deleted {name}", severity="information")

    def action_new_collection(self) -> None:
        screen = _NewCollectionScreen()
        self.app.push_screen(screen, callback=self._on_new_collection_named)

    def _on_new_collection_named(self, name: str | None) -> None:
        if not name:
            return
        if name in self._config.collections:
            self.app.notify(f"Collection {name} already exists.", severity="warning")
            return
        from acorn.config import CollectionConfig

        empty = CollectionConfig(sources=[])
        self._config.collections[name] = empty
        self._initial[name] = []
        self._rebuild_tree(focus=(name, None))
        self.app.notify(
            f"Created {name}. Press 'a' to add a source, 's' to save.",
            severity="information",
        )


class _DeleteConfirmScreen(Screen[bool]):
    """Tiny y/N confirmation modal."""

    BINDINGS = [  # noqa: RUF012
        Binding("y,Y", "yes", "Yes", show=True),
        Binding("n,N,escape", "no", "No", show=True),
    ]

    CSS = """
    _DeleteConfirmScreen { align: center middle; background: $surface 80%; }
    #confirm_box { width: 60%; height: auto; border: round $error; padding: 1; background: $surface; }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(self._message),
            Static("[y] yes   [N/Esc] no", classes="footer_hint"),
            id="confirm_box",
        )

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class _NewCollectionScreen(Screen[str | None]):
    """Tiny name-prompt for creating an empty collection."""

    BINDINGS = [  # noqa: RUF012
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    CSS = """
    _NewCollectionScreen { align: center middle; background: $surface 80%; }
    #new_collection_box { width: 60%; height: auto; border: round $accent; padding: 1; background: $surface; }
    """

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("New collection name:"),
            Input(id="new_collection_name", placeholder="e.g. research"),
            Static("[Enter] create   [Esc] cancel", classes="footer_hint"),
            id="new_collection_box",
        )

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        name = ev.value.strip()
        self.dismiss(name or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SourceEditScreen(Screen[dict[str, object] | None]):
    """Modal for editing one source. Returns the edited fields (or None
    if cancelled) via :meth:`Screen.dismiss`. The parent
    :class:`CollectionsScreen` applies the change to its in-memory
    Config and re-renders.
    """

    BINDINGS = [  # noqa: RUF012
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("ctrl+s", "save", "Save", show=True),
    ]

    CSS = """
    SourceEditScreen { align: center middle; background: $surface 80%; }
    #source_edit_box { width: 80%; height: auto; border: round $accent; padding: 1; background: $surface; }
    #source_edit_box Input { margin-bottom: 1; }
    #filter_parse_status { color: $success; }
    .filter_parse_error { color: $error; }
    #frontmatter_sample { height: 6; margin-bottom: 1; border: round $surface; }
    """

    def __init__(
        self,
        *,
        title: str,
        path: str,
        includes: list[str],
        excludes: list[str],
        frontmatter_filter: str | None,
    ) -> None:
        super().__init__()
        self._title = title
        self._initial = {
            "path": path,
            "includes": ",".join(includes),
            "excludes": ",".join(excludes),
            "frontmatter_filter": frontmatter_filter or "",
        }

    def compose(self) -> ComposeResult:
        with Vertical(id="source_edit_box"):
            yield Static(f"Edit source — {self._title}", classes="editor_heading")
            yield Static("Path:")
            yield Input(value=self._initial["path"], id="source_path_input")
            yield Static("Includes (comma-separated globs):")
            yield Input(value=self._initial["includes"], id="source_includes_input")
            yield Static("Excludes (comma-separated globs):")
            yield Input(value=self._initial["excludes"], id="source_excludes_input")
            yield Static("Frontmatter filter (DSL):")
            yield Input(value=self._initial["frontmatter_filter"], id="source_filter_input")
            yield Static("✓ filter parses", id="filter_parse_status")
            yield Static("Test against pasted frontmatter:")
            yield TextArea("", id="frontmatter_sample", classes="frontmatter_sample")
            yield Static("(no sample)", id="frontmatter_match_status")
            yield Static("ctrl+s save · esc cancel", classes="footer_hint")

    def on_input_changed(self, ev: Input.Changed) -> None:
        if ev.input.id != "source_filter_input":
            return
        from acorn.filter_dsl import parse_or_error

        text = ev.value
        status = self.query_one("#filter_parse_status", Static)
        if not text.strip():
            status.update("(no filter)")
            status.remove_class("filter_parse_error")
        else:
            _pred, err = parse_or_error(text)
            if err is None:
                status.update("✓ filter parses")
                status.remove_class("filter_parse_error")
            else:
                status.update(f"✗ col {err.column}: {err.message}")
                status.add_class("filter_parse_error")
        self._refresh_match_status()

    def on_text_area_changed(self, ev: TextArea.Changed) -> None:
        if ev.text_area.id != "frontmatter_sample":
            return
        self._refresh_match_status()

    def _refresh_match_status(self) -> None:
        from acorn.filter_dsl import parse_or_error
        from acorn.frontmatter import FrontmatterParseError, read_frontmatter_from_text

        match = self.query_one("#frontmatter_match_status", Static)
        sample = self.query_one("#frontmatter_sample", TextArea).text
        filter_text = self.query_one("#source_filter_input", Input).value.strip()
        if not sample.strip():
            match.update("(no sample)")
            match.remove_class("cs_match")
            match.remove_class("cs_no_match")
            return
        try:
            fm: dict[str, object] = read_frontmatter_from_text(sample) or {}
        except FrontmatterParseError as e:
            match.update(f"✗ frontmatter parse error: {e}")
            match.add_class("cs_no_match")
            match.remove_class("cs_match")
            return
        if not filter_text:
            match.update("(no filter — sample is parsed but no predicate)")
            match.remove_class("cs_match")
            match.remove_class("cs_no_match")
            return
        pred, err = parse_or_error(filter_text)
        if err is not None or pred is None:
            match.update(f"✗ filter syntax: col {err.column}" if err else "✗")
            match.add_class("cs_no_match")
            match.remove_class("cs_match")
            return
        if pred(fm):
            match.update("✓ matches filter")
            match.add_class("cs_match")
            match.remove_class("cs_no_match")
        else:
            match.update("✗ no match")
            match.add_class("cs_no_match")
            match.remove_class("cs_match")

    def action_save(self) -> None:
        from acorn.filter_dsl import parse_or_error

        filter_text = self.query_one("#source_filter_input", Input).value.strip()
        if filter_text:
            _pred, err = parse_or_error(filter_text)
            if err is not None:
                # Refuse to save: surface a notify, leave the modal open.
                self.app.notify(
                    f"col {err.column}: {err.message}",
                    severity="error",
                    title="Filter syntax",
                )
                return
        path = _strip_wrapping_quotes(self.query_one("#source_path_input", Input).value.strip())
        if not path:
            self.app.notify("path is required", severity="error", title="Invalid path")
            return
        expanded = Path(path).expanduser()
        if not expanded.exists():
            self.app.notify(
                f"path does not exist: {expanded}",
                severity="error",
                title="Invalid path",
            )
            return
        result: dict[str, object] = {
            "path": path,
            "includes": [
                s.strip()
                for s in self.query_one("#source_includes_input", Input).value.split(",")
                if s.strip()
            ],
            "excludes": [
                s.strip()
                for s in self.query_one("#source_excludes_input", Input).value.split(",")
                if s.strip()
            ],
            "frontmatter_filter": filter_text or None,
        }
        self.dismiss(result)

    def action_cancel(self) -> None:
        self.dismiss(None)

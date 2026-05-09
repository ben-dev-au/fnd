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
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Input, Static, TextArea

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
    """Top-level Collections form. Pushed from the main app via F3."""

    BINDINGS = [  # noqa: RUF012 — Textual class-list pattern
        Binding("escape", "close", "Close", show=True),
        Binding("j,down", "list_next", "Next", show=False),
        Binding("k,up", "list_prev", "Prev", show=False),
        Binding("e", "edit_source", "Edit source", show=True),
        Binding("J", "source_next", "Source ↓", show=False),
        Binding("K", "source_prev", "Source ↑", show=False),
        Binding("a", "add_source", "Add source", show=True),
        Binding("x", "remove_source", "Remove source", show=True),
        Binding("s", "save", "Save", show=True),
        Binding("d", "delete_collection", "Delete", show=True),
        Binding("n", "new_collection", "New", show=True),
    ]

    CSS = """
    CollectionsScreen { background: $surface; }
    #collections_title { dock: top; height: 1; padding: 0 1; background: $panel; color: $accent; text-style: bold; }
    #collections_body { width: 100%; height: 1fr; }
    #collections_list_pane { width: 1fr; height: 1fr; border: round $primary; padding: 1; }
    #collections_editor_pane { width: 2fr; height: 1fr; border: round $primary; padding: 1; }
    """

    def __init__(self, config: Config, *, config_path: Path) -> None:
        super().__init__()
        self._config = config
        self._config_path = config_path
        # Default selection: first collection alphabetically.
        self._selected: str | None = (
            sorted(config.collections.keys())[0] if config.collections else None
        )
        self._source_cursor: int = 0
        # Deep-copy the source list per collection at form open. Compared
        # against the live ``self._config`` on save to decide whether a
        # reindex is needed.
        self._initial: dict[str, list[SourceConfig]] = {
            name: deepcopy(c.sources) for name, c in config.collections.items()
        }

    def compose(self) -> ComposeResult:
        yield Static("Collections", id="collections_title")
        with Horizontal(id="collections_body"):
            with Vertical(id="collections_list_pane"):
                yield from self._collection_rows()
            with Vertical(id="collections_editor_pane"):
                yield from self._editor_rows()
        yield Footer()

    def _collection_rows(self) -> ComposeResult:
        names = sorted(self._config.collections.keys())
        if not names:
            yield Static("(no collections — press n to add one)")
            return
        for name in names:
            collection = self._config.collections[name]
            count = len(collection.sources)
            marker = "▸ " if name == self._selected else "  "
            label = (
                f"{marker}{name}  ({count} source{'s' if count != 1 else ''})"
                f"  [{collection.ranking_profile}]"
            )
            yield Static(label, classes="collection_row")

    def _editor_rows(self) -> ComposeResult:
        if self._selected is None:
            yield Static("Select a collection on the left, or press n to add a new one.")
            return
        c = self._config.collections[self._selected]
        yield Static(f"Editing: {self._selected}", classes="editor_heading")
        yield Static(f"Ranking: {c.ranking_profile}")
        yield Static("Sources:")
        if not c.sources:
            yield Static("  (none — press a to add a source)")
            return
        for i, s in enumerate(c.sources):
            marker = "▸ " if i == self._source_cursor else "  "
            yield Static(f"{marker}{i + 1}. {s.path}", classes="source_row")
            if s.includes:
                yield Static(f"     includes: {', '.join(s.includes)}")
            if s.excludes:
                yield Static(f"     excludes: {', '.join(s.excludes)}")
            if s.frontmatter_filter:
                yield Static(f"     filter:   {s.frontmatter_filter}")

    def action_close(self) -> None:
        self.dismiss(None)

    def action_list_next(self) -> None:
        names = sorted(self._config.collections.keys())
        if not names or self._selected is None:
            return
        i = names.index(self._selected)
        self._selected = names[(i + 1) % len(names)]
        self._source_cursor = 0
        self._refresh()

    def action_list_prev(self) -> None:
        names = sorted(self._config.collections.keys())
        if not names or self._selected is None:
            return
        i = names.index(self._selected)
        self._selected = names[(i - 1) % len(names)]
        self._source_cursor = 0
        self._refresh()

    def action_edit_source(self) -> None:
        if self._selected is None:
            return
        c = self._config.collections[self._selected]
        if not c.sources:
            return
        s = c.sources[self._source_cursor]
        screen = SourceEditScreen(
            title=f"{self._selected} / {self._source_cursor + 1}",
            path=str(s.path),
            includes=list(s.includes),
            excludes=list(s.excludes),
            frontmatter_filter=s.frontmatter_filter,
        )
        idx = self._source_cursor
        self.app.push_screen(screen, callback=lambda r: self._apply_source_edit(idx, r))

    def action_source_next(self) -> None:
        if self._selected is None:
            return
        c = self._config.collections[self._selected]
        if c.sources:
            self._source_cursor = (self._source_cursor + 1) % len(c.sources)
        self._refresh()

    def action_source_prev(self) -> None:
        if self._selected is None:
            return
        c = self._config.collections[self._selected]
        if c.sources:
            self._source_cursor = (self._source_cursor - 1) % len(c.sources)
        self._refresh()

    def action_add_source(self) -> None:
        if self._selected is None:
            return
        screen = SourceEditScreen(
            title=f"{self._selected} / new",
            path="",
            includes=[],
            excludes=[],
            frontmatter_filter=None,
        )
        self.app.push_screen(screen, callback=self._on_new_source_dismissed)

    def _on_new_source_dismissed(self, result: dict[str, object] | None) -> None:
        if result is None or self._selected is None:
            return
        c = self._config.collections[self._selected]
        c.sources.append(
            SourceConfig(
                path=Path(str(result["path"])),
                includes=list(result["includes"]),  # type: ignore[arg-type]
                excludes=list(result["excludes"]),  # type: ignore[arg-type]
                frontmatter_filter=result.get("frontmatter_filter"),  # type: ignore[arg-type]
            )
        )
        self._source_cursor = len(c.sources) - 1
        self._refresh()

    def action_remove_source(self) -> None:
        if self._selected is None:
            return
        c = self._config.collections[self._selected]
        if not c.sources:
            return
        del c.sources[self._source_cursor]
        if c.sources:
            self._source_cursor = min(self._source_cursor, len(c.sources) - 1)
        else:
            self._source_cursor = 0
        self._refresh()

    def action_save(self) -> None:
        if self._selected is None:
            return
        from acorn.config import write_collection
        from acorn.index import build_index_from_config

        c = self._config.collections[self._selected]
        write_collection(
            config_path=self._config_path,
            name=self._selected,
            collection=c,
        )
        # Did anything structural change? If so, reindex synchronously.
        if self._needs_reindex(self._selected):
            self.app.notify(
                f"Reindexing {self._selected}…",
                severity="information",
                timeout=2,
            )
            try:
                n = build_index_from_config(
                    config=c,
                    collection=self._selected,
                    index_dir=self.app._index_dir,  # type: ignore[attr-defined]
                    rebuild=True,
                )
                self.app.notify(
                    f"Indexed {n} chunks for {self._selected}.",
                    severity="information",
                )
            except Exception as e:
                self.app.notify(f"Reindex failed: {e}", severity="error")
        else:
            self.app.notify(f"Saved {self._selected}", severity="information")
        # Refresh snapshot so subsequent saves diff against the new state.
        self._initial[self._selected] = deepcopy(c.sources)

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

    def _apply_source_edit(self, index: int, result: dict[str, object] | None) -> None:
        if result is None or self._selected is None:
            return
        c = self._config.collections[self._selected]
        new_source = SourceConfig(
            path=Path(str(result["path"])),
            includes=list(result["includes"]),  # type: ignore[arg-type]
            excludes=list(result["excludes"]),  # type: ignore[arg-type]
            frontmatter_filter=result.get("frontmatter_filter"),  # type: ignore[arg-type]
            follow_symlinks=c.sources[index].follow_symlinks,
        )
        c.sources[index] = new_source
        self._refresh()

    def _refresh(self) -> None:
        """Re-render both panes; cheap because the form is small."""
        list_pane = self.query_one("#collections_list_pane", Vertical)
        editor_pane = self.query_one("#collections_editor_pane", Vertical)
        list_pane.remove_children()
        editor_pane.remove_children()
        list_pane.mount_all(self._collection_rows())
        editor_pane.mount_all(self._editor_rows())

    def action_delete_collection(self) -> None:
        if self._selected is None:
            return
        name = self._selected
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
        # 4. Update selection.
        names = sorted(self._config.collections.keys())
        self._selected = names[0] if names else None
        self._source_cursor = 0
        self._refresh()
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
        self._selected = name
        self._source_cursor = 0
        self._refresh()
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

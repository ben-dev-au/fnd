"""TUI Collections form (§5.5e-3).

Full-screen Textual ``Screen`` that lists configured collections and lets
the user edit, add, or delete them without leaving the TUI. Saves persist
to ``config.toml`` via :func:`acorn.config.write_collection`, which uses
``tomlkit`` so user-authored comments survive the round-trip.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Static

from acorn.config import Config


class CollectionsScreen(Screen[None]):
    """Top-level Collections form. Pushed from the main app via F3."""

    BINDINGS = [  # noqa: RUF012 — Textual class-list pattern
        Binding("escape", "close", "Close", show=True),
        Binding("j,down", "list_next", "Next", show=False),
        Binding("k,up", "list_prev", "Prev", show=False),
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
            yield Static(label, classes="collection_row", id=f"collection_row_{name}")

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
        for i, s in enumerate(c.sources, start=1):
            yield Static(f"  {i}. {s.path}", classes="source_row")
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
        self._refresh()

    def action_list_prev(self) -> None:
        names = sorted(self._config.collections.keys())
        if not names or self._selected is None:
            return
        i = names.index(self._selected)
        self._selected = names[(i - 1) % len(names)]
        self._refresh()

    def _refresh(self) -> None:
        """Re-render both panes; cheap because the form is small."""
        list_pane = self.query_one("#collections_list_pane", Vertical)
        editor_pane = self.query_one("#collections_editor_pane", Vertical)
        list_pane.remove_children()
        editor_pane.remove_children()
        list_pane.mount_all(self._collection_rows())
        editor_pane.mount_all(self._editor_rows())

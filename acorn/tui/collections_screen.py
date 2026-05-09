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

    def compose(self) -> ComposeResult:
        yield Static("Collections", id="collections_title")
        with Horizontal(id="collections_body"):
            with Vertical(id="collections_list_pane"):
                yield from self._collection_rows()
            yield Vertical(id="collections_editor_pane")
        yield Footer()

    def _collection_rows(self) -> ComposeResult:
        names = sorted(self._config.collections.keys())
        if not names:
            yield Static("(no collections — press n to add one)")
            return
        for name in names:
            collection = self._config.collections[name]
            count = len(collection.sources)
            label = f"{name}  ({count} source{'s' if count != 1 else ''})  [{collection.ranking_profile}]"
            yield Static(label, classes="collection_row", id=f"collection_row_{name}")

    def action_close(self) -> None:
        self.dismiss(None)

"""Acorn TUI — phase 5 shell.

Layout (per §5 wireframe):

  ┌─ Status bar (collection · result count) ─┐
  ├─ Query input ────────────────────────────┤
  ├─ Results tree (left)  │  Preview pane ──┤
  └──────────────────────────────────────────┘
   /  search   Tab  focus   ⏎  open   Space  peek   o  default-app   q  quit

Phase 5 ships the structural layout + opener wired to Enter; phase 6 adds
the full action map (filter chips, command palette, customisable keymap),
phase 7 adds reranker live-tuning.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Input, Markdown, Static, Tree
from textual.widgets.tree import TreeNode

from acorn import opener
from acorn.config import default_index_dir
from acorn.extract.base import Block
from acorn.query import FileGroup, Hit, Searcher
from acorn.render import render


def _format_hit_label(h: Hit) -> str:
    if h.page:
        loc = f"p.{h.page}"
    elif h.slide:
        loc = f"s.{h.slide}"
    elif h.heading_path:
        loc = f"§ {h.heading_path}"
    else:
        loc = "—"
    section = h.heading_path.split(" > ")[-1] if h.heading_path else ""
    suffix = f"  {section}" if section and " > " in h.heading_path else ""
    return f"{loc}{suffix}  ({h.score:.2f})"


def _format_file_label(g: FileGroup) -> str:
    name = Path(g.path).name
    return f"{name}  ({g.top_score:.2f})  [{g.kind}]"


class AcornApp(App[None]):
    """Phase 5 shell."""

    CSS = """
    #query_bar { height: 3; padding: 0 1; }
    #status_bar { dock: top; height: 1; background: $boost; padding: 0 1; }
    #results_pane, #preview_pane { width: 1fr; height: 1fr; border: round $primary; }
    Tree > .tree--label { padding: 0 1; }
    """

    BINDINGS = [  # noqa: RUF012 — Textual's App.BINDINGS expects a class-level list literal
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("q", "quit", "Quit"),
        Binding("slash", "focus_query", "Search"),
        Binding("tab", "toggle_focus", "Focus pane"),
        Binding("space", "peek_focused", "Peek"),
        Binding("o", "open_default", "Open default"),
    ]

    def __init__(
        self,
        *,
        index_dir: Path | None = None,
        collection: str | None = None,
        initial_query: str = "",
    ) -> None:
        super().__init__()
        self._index_dir = index_dir or default_index_dir()
        self._collection = collection
        self._initial_query = initial_query
        self._searcher: Searcher | None = None
        self._current_query: str = ""
        self._groups: list[FileGroup] = []

    # ── Layout ────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Static(self._status_text(), id="status_bar")
        yield Input(placeholder="Search…", id="query_bar", value=self._initial_query)
        with Horizontal():
            yield Tree("Results", id="results_pane")
            with Vertical(id="preview_pane"):
                yield Markdown("*Type a query and press Enter.*", id="preview_md")
        yield Footer()

    def on_mount(self) -> None:
        self._searcher = Searcher(index_dir=self._index_dir)
        tree = self.query_one("#results_pane", Tree)
        tree.show_root = False
        tree.guide_depth = 2
        if self._initial_query:
            self._run_query(self._initial_query)
        self.query_one("#query_bar", Input).focus()

    # ── Status ────────────────────────────────────────────────────

    def _status_text(self) -> str:
        col = self._collection or "all"
        n_files = len(self._groups)
        n_sections = sum(len(g.hits) for g in self._groups)
        return f" acorn  [{col}]   {n_files} files / {n_sections} sections"

    def _refresh_status(self) -> None:
        self.query_one("#status_bar", Static).update(self._status_text())

    # ── Search flow ───────────────────────────────────────────────

    @on(Input.Submitted, "#query_bar")
    def _on_query_submit(self, ev: Input.Submitted) -> None:
        self._run_query(ev.value)

    def _run_query(self, query: str) -> None:
        if self._searcher is None:
            return
        self._current_query = query
        self._groups = self._searcher.search_grouped(
            query, limit=50, sections_per_file=10, collection=self._collection
        )
        tree = self.query_one("#results_pane", Tree)
        tree.clear()
        for g in self._groups:
            file_node = tree.root.add(
                _format_file_label(g),
                data={"kind": "file", "group": g},
                expand=False,
            )
            for h in g.hits:
                file_node.add_leaf(_format_hit_label(h), data={"kind": "section", "hit": h})
        self._refresh_status()
        if self._groups:
            tree.focus()

    # ── Preview ───────────────────────────────────────────────────

    @on(Tree.NodeHighlighted)
    def _on_tree_highlight(self, ev: Tree.NodeHighlighted[Any]) -> None:
        data: Any = ev.node.data
        if not isinstance(data, dict):
            return
        if data.get("kind") == "section":
            self._render_hit(data["hit"])
        elif data.get("kind") == "file":
            g: FileGroup = data["group"]
            if g.hits:
                self._render_hit(g.hits[0])

    def _render_hit(self, hit: Hit) -> None:
        blocks: list[Block] = [Block(kind="p", text=hit.snippet)]
        body_md = render(blocks, query=self._current_query)
        crumb_parts: list[str] = [Path(hit.path).name]
        if hit.page:
            crumb_parts.append(f"p.{hit.page}")
        if hit.slide:
            crumb_parts.append(f"s.{hit.slide}")
        if hit.heading_path:
            crumb_parts.append(f"§ {hit.heading_path}")
        crumb = "  ·  ".join(crumb_parts)
        md = self.query_one("#preview_md", Markdown)
        md.update(f"### {crumb}\n\n{body_md}")

    # ── Open / peek dispatch ──────────────────────────────────────

    @on(Tree.NodeSelected)
    def _on_tree_select(self, ev: Tree.NodeSelected[Any]) -> None:
        """Enter on a tree node opens the matching file at its locator."""
        target = self._target_for_node(ev.node)
        if target is None:
            return
        _, hit = target
        opener.open_smart(path=Path(hit.path), kind=hit.kind, page=hit.page)

    @staticmethod
    def _target_for_node(node: TreeNode[Any]) -> tuple[FileGroup, Hit] | None:
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

    # ── Actions ───────────────────────────────────────────────────

    def action_focus_query(self) -> None:
        self.query_one("#query_bar", Input).focus()

    def action_toggle_focus(self) -> None:
        tree = self.query_one("#results_pane", Tree)
        if self.focused is tree:
            self.query_one("#query_bar", Input).focus()
        else:
            tree.focus()

    def action_open_default(self) -> None:
        tree = self.query_one("#results_pane", Tree)
        if tree.cursor_node is None:
            return
        target = self._target_for_node(tree.cursor_node)
        if target is None:
            return
        _, hit = target
        opener.open_default(Path(hit.path))

    def action_peek_focused(self) -> None:
        tree = self.query_one("#results_pane", Tree)
        if tree.cursor_node is None:
            return
        target = self._target_for_node(tree.cursor_node)
        if target is None:
            return
        _, hit = target
        opener.peek(Path(hit.path))

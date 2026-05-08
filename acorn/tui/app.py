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
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Input, Label, Markdown, SelectionList, Static, Tree
from textual.widgets.selection_list import Selection
from textual.widgets.tree import TreeNode

from acorn import opener
from acorn.config import default_index_dir
from acorn.query import FileChunk, FileGroup, Hit, Searcher
from acorn.render import render_document
from acorn.tui.actions import REGISTRY, Keymap, load_keymap, resolve_command


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


def _estimate_block_lines(block: object) -> int:
    """Rough line count for a body_struct block — used by scroll positioning."""
    text = getattr(block, "text", "") or ""
    if not text:
        return 1
    # Wrap-friendly heuristic: 1 line per 80 chars + count newlines.
    return max(1, text.count("\n") + 1 + len(text) // 80)


def _short_label(action_id: str) -> str:
    """Footer-hint label for an action — first noun of the description."""
    for a in REGISTRY:
        if a.id == action_id:
            return a.description.split(".")[0].split(" ")[0].rstrip(",")
    return action_id


class AcornApp(App[None]):
    """Phase 5 shell."""

    CSS = """
    #query_bar { height: 3; padding: 0 1; }
    #status_bar { dock: top; height: 1; background: $boost; padding: 0 1; }
    #results_pane { width: 1fr; height: 1fr; border: round $primary; }
    #preview_pane { width: 2fr; height: 1fr; border: round $primary; }
    #preview_md { padding: 0 1; }
    #help_overlay {
        layer: overlay;
        background: $surface;
        border: round $accent;
        margin: 2 4 3 4;
        padding: 1 2 2 2;
    }
    #cmd_palette {
        dock: bottom;
        height: 3;
        padding: 0 1;
        background: $boost;
    }
    #collection_picker {
        layer: overlay;
        background: $surface;
        border: round $accent;
        margin: 4 8;
        padding: 1 2 2 2;
        height: auto;
    }
    Tree > .tree--label { padding: 0 1; }
    """

    # BINDINGS is built from the action registry at import time so footer
    # hints, help overlay, and runtime keymap all share one source.
    BINDINGS = [  # noqa: RUF012 — Textual's App.BINDINGS expects a class-level list literal
        Binding("ctrl+c", "quit", "Quit", show=False),
        *(
            Binding(key, action_id, _short_label(action_id))
            for key, action_id in load_keymap().bindings.items()
        ),
    ]

    def __init__(
        self,
        *,
        index_dir: Path | None = None,
        collection: str | None = None,
        initial_query: str = "",
        keymap: Keymap | None = None,
    ) -> None:
        super().__init__()
        self._index_dir = index_dir or default_index_dir()
        # Active collections — list[] supports multi-select via the picker.
        # Empty list = "all collections" (no scope filter).
        self._collections: list[str] = [collection] if collection else []
        self._initial_query = initial_query
        self._searcher: Searcher | None = None
        self._current_query: str = ""
        self._groups: list[FileGroup] = []
        self._acorn_keymap = keymap or load_keymap()
        # Last `:command` palette result, exposed for tests.
        self.last_palette_result: str | None = None
        # Cache of (parent_id) → list[FileChunk] so we don't re-fetch the
        # full document on every cursor move within the same file. Keyed by
        # parent_id, invalidated on new query.
        self._chunk_cache: dict[str, list[FileChunk]] = {}

    # ── Layout ────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Static(self._status_text(), id="status_bar")
        yield Input(placeholder="Search…", id="query_bar", value=self._initial_query)
        with Horizontal():
            yield Tree("Results", id="results_pane")
            # Preview pane is a VerticalScroll wrapping Markdown so users can
            # scroll the full document.
            with VerticalScroll(id="preview_pane"):
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
        col = ",".join(self._collections) if self._collections else "all"
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
        # Multi-collection scoping: prefix with `c:a,b` so the DSL pre-pass
        # builds the correct (collection:"a" OR collection:"b") filter.
        if len(self._collections) >= 2:
            scoped_query = f"c:{','.join(self._collections)} {query}"
            single_col = None
        else:
            scoped_query = query
            single_col = self._collections[0] if self._collections else None
        self._groups = self._searcher.search_grouped(
            scoped_query, limit=50, sections_per_file=10, collection=single_col
        )
        # New query → invalidate the per-file chunk cache.
        self._chunk_cache.clear()
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
            hit: Hit = data["hit"]
            self._render_full_doc(hit.parent_id, focus_chunk_seq=hit.chunk_seq)
        elif data.get("kind") == "file":
            g: FileGroup = data["group"]
            top = g.hits[0] if g.hits else None
            self._render_full_doc(g.parent_id, focus_chunk_seq=top.chunk_seq if top else 0)

    def _render_full_doc(self, parent_id: str, *, focus_chunk_seq: int) -> None:
        """Render the full document for ``parent_id`` and (best-effort) scroll
        the preview to the section identified by ``focus_chunk_seq``."""
        if self._searcher is None:
            return
        chunks = self._chunk_cache.get(parent_id)
        if chunks is None:
            chunks = self._searcher.get_file_chunks(parent_id)
            self._chunk_cache[parent_id] = chunks
        if not chunks:
            return
        path = chunks[0].path
        crumb = Path(path).name
        body_md = render_document(list(chunks), query=self._current_query)
        md = self.query_one("#preview_md", Markdown)
        md.update(f"# {crumb}\n\n{body_md}")
        # Scroll the preview to the focused chunk's approximate offset.
        # Each chunk is preceded by a "## …" header; we count those to
        # estimate where to scroll the underlying VerticalScroll.
        self._scroll_preview_to_chunk(chunks, focus_chunk_seq)

    def _scroll_preview_to_chunk(self, chunks: list[FileChunk], focus_chunk_seq: int) -> None:
        """Approximate scroll-to-chunk by counting blocks before the target."""
        scroll = self.query_one("#preview_pane", VerticalScroll)
        # If we're at the first chunk, just scroll home.
        if focus_chunk_seq <= chunks[0].chunk_seq:
            scroll.scroll_home(animate=False)
            return
        # Estimate visual lines = sum of block-count + 2 (header + divider) per
        # preceding chunk. This is approximate but stable enough for orientation;
        # a precise anchor system can land in a follow-up.
        line_estimate = 1  # account for the file-title heading
        for c in chunks:
            if c.chunk_seq >= focus_chunk_seq:
                break
            line_estimate += 2  # "## header" + blank
            line_estimate += sum(_estimate_block_lines(b) for b in c.blocks)
            line_estimate += 2  # divider + blank
        scroll.scroll_to(y=line_estimate, animate=False)

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

    def action_open_focused(self) -> None:
        """Open the current tree node — same as `Tree.NodeSelected`."""
        tree = self.query_one("#results_pane", Tree)
        if tree.cursor_node is None:
            return
        target = self._target_for_node(tree.cursor_node)
        if target is None:
            return
        _, hit = target
        opener.open_smart(path=Path(hit.path), kind=hit.kind, page=hit.page)

    def action_open_collection_picker(self) -> None:
        """Pop a SelectionList of all configured collections; user toggles
        which to include in the search scope, presses Enter to apply."""
        from acorn.config import load as load_config

        existing = self.query("#collection_picker")
        if existing:
            for w in existing:
                w.remove()
            return
        cfg = load_config()
        names = sorted(cfg.collections)
        if not names:
            # Show a tiny note and bail.
            note = Vertical(
                Label("No collections configured. Run `acorn config edit`."),
                id="collection_picker",
            )
            self.mount(note)
            return
        selections = [Selection(name, name, name in self._collections) for name in names]
        sel_list = SelectionList[str](*selections, id="collection_selection")
        wrapper = Vertical(
            Label("Collections (Space toggles, Enter applies)"),
            sel_list,
            id="collection_picker",
        )
        self.mount(wrapper)
        sel_list.focus()

    @on(SelectionList.SelectedChanged, "#collection_selection")
    def _on_collection_selection_changed(self, ev: SelectionList.SelectedChanged[str]) -> None:
        """Live-update the active collection scope as the user toggles."""
        self._collections = list(ev.selection_list.selected)
        self._refresh_status()

    def action_show_help(self) -> None:
        """Toggle a help overlay listing every action and its key."""
        existing = self.query("#help_overlay")
        if existing:
            for w in existing:
                w.remove()
            return
        from textual.widgets import Markdown as _Md

        lines: list[str] = ["# Help", "", "| key | command | description |", "|---|---|---|"]
        for a in REGISTRY:
            key = self._acorn_keymap.for_action(a.id) or "—"
            cmd = f":{a.palette_command}"
            lines.append(f"| `{key}` | `{cmd}` | {a.description} |")
        overlay = Vertical(_Md("\n".join(lines)), id="help_overlay")
        self.mount(overlay)

    def action_open_command_palette(self) -> None:
        """Pop a one-shot input that runs ``resolve_command`` on submit."""

        existing = self.query("#cmd_palette")
        if existing:
            for w in existing:
                w.remove()
            return
        palette_input = Input(placeholder="Command…", id="cmd_palette_input")
        wrapper = Vertical(palette_input, id="cmd_palette")
        self.mount(wrapper)
        palette_input.focus()

    @on(Input.Submitted, "#cmd_palette_input")
    def _on_palette_submit(self, ev: Input.Submitted) -> None:
        name = ev.value.strip().lstrip(":")
        action = resolve_command(name)
        # Close the palette regardless.
        for w in self.query("#cmd_palette"):
            w.remove()
        if action is None:
            self.last_palette_result = f"unknown:{name}"
            return
        self.last_palette_result = action.id
        # Run the action by name.
        method = getattr(self, f"action_{action.id}", None)
        if callable(method):
            method()

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
from textual.widgets import Input, Label, SelectionList, Static, Tree
from textual.widgets.selection_list import Selection
from textual.widgets.tree import TreeNode

from acorn import opener
from acorn.config import Config, default_index_dir
from acorn.query import FileChunk, FileGroup, Hit, Searcher
from acorn.render import render_chunk_pieces
from acorn.rerank import RankingProfile, profile_from_config
from acorn.tui.actions import REGISTRY, Keymap, load_keymap, resolve_command

_PASS_GLYPHS = {0: "●", 1: "~", 2: "⊕", 3: "❝"}


def _score_bar(  # pyright: ignore[reportUnusedFunction]
    *,
    score: float,
    max_score: float,
    width: int = 5,
) -> str:
    """Pure utility kept for the legacy test surface.

    The TUI no longer draws score bars — the user's feedback on the
    eighth-block and full-block variants was that they read as visual
    noise. The current label formatters use :func:`_score_style`
    instead, colouring the numeric score in line with the theme.
    """
    if max_score <= 0:
        return " " * width
    ratio = max(0.0, min(1.0, score / max_score))
    full = round(ratio * width)
    return "█" * full + " " * (width - full)


def _score_style(score: float, max_score: float) -> str:
    """Rich-style spec for a numeric score, graded by relative position.

    Walks the tokyo-night accent palette from a vivid green (top tier)
    through cyan and accent-blue down to a muted slate. The score is
    the only place we lean on colour for ranking signal, so the steps
    are saturated enough to read at a glance without becoming a
    stoplight.
    """
    if max_score <= 0:
        return "dim"
    ratio = max(0.0, min(1.0, score / max_score))
    if ratio >= 0.85:
        return "bold #9ece6a"  # tokyo-night green — leader
    if ratio >= 0.6:
        return "#7dcfff"  # cyan
    if ratio >= 0.35:
        return "#7aa2f7"  # accent blue (theme default)
    if ratio >= 0.15:
        return "#bb9af7"  # cool magenta — fades from accent
    return "dim #565f89"


def _build_label(text: str, score: float, max_score: float) -> Any:
    """Tree label combining a coloured numeric score (left, fixed width)
    with the file/section text (right, may truncate cleanly).

    Score-first layout means the colour-coded ranking signal is always
    visible regardless of filename length — long titles truncate
    against the right edge of the pane without ever eating the score.
    """
    from rich.text import Text

    label = Text()
    if max_score > 0 and score > 0:
        label.append(f"{score:5.2f}", style=_score_style(score, max_score))
        label.append("  ")
    else:
        label.append(" " * 7)
    label.append(text)
    return label


def _format_hit_label(h: Hit, *, max_score: float = 0.0) -> Any:
    if h.page:
        loc = f"p.{h.page}"
    elif h.slide:
        loc = f"s.{h.slide}"
    elif h.heading_path:
        loc = f"§ {h.heading_path}"
    else:
        # Markdown / TXT chunks with no headings still need a locator —
        # fall back to the chunk sequence so every section row carries
        # a position marker the user can act on.
        loc = f"chunk {h.chunk_seq + 1}"
    section = h.heading_path.split(" > ")[-1] if h.heading_path else ""
    suffix = f"  {section}" if section and " > " in h.heading_path else ""
    # Per-pass glyph (§9c): exact / fuzzy / synonym. Suppressed for the
    # exact pass to keep the common case visually quiet.
    glyph = _PASS_GLYPHS.get(h.pass_index, "")
    pass_marker = f" {glyph}" if h.pass_index > 0 else ""
    return _build_label(f"{loc}{suffix}{pass_marker}", h.score, max_score)


def _format_file_label(g: FileGroup, *, max_score: float = 0.0) -> Any:
    return _build_label(Path(g.path).name, g.top_score, max_score)


def _short_label(action_id: str) -> str:
    """Footer-hint label for an action — uses Action.footer_label when set,
    otherwise falls back to the first word of the description."""
    for a in REGISTRY:
        if a.id == action_id:
            if a.footer_label:
                return a.footer_label
            return a.description.split(".")[0].split(" ")[0].rstrip(",")
    return action_id


def _action_show(action_id: str) -> bool:
    for a in REGISTRY:
        if a.id == action_id:
            return a.show_in_footer
    return True


_KEY_HINT_GLYPHS = {
    "slash": "/",
    "colon": ":",
    "question_mark": "?",
    "space": "Spc",
    "tab": "Tab",
    "enter": "Enter",
    "escape": "Esc",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
}


def _format_key_hint(key: str) -> str:
    """Pretty-print a binding key for the footer hint row.

    Plain ASCII labels — Unicode arrow / return glyphs render
    unevenly across terminals and the user reported them looking
    'malformed and backwards' in the live UI."""
    return _KEY_HINT_GLYPHS.get(key, key)


class _PickerSelectionList(SelectionList[str]):
    """SelectionList with Enter rebound to "apply" rather than "toggle".

    Textual's ``OptionList.BINDINGS`` maps ``enter`` to ``select``, and
    ``SelectionList.action_select`` toggles the option — meaning
    Space-then-Enter (the natural "select then confirm" flow) silently
    untoggles the user's choice. We override Enter to leave the picker
    intact and dismiss it via the app's overlay handler.
    """

    BINDINGS = [Binding("enter", "apply", "Apply", show=False, priority=True)]  # noqa: RUF012

    def action_apply(self) -> None:
        # Defer to the app's overlay-dismiss action, which removes the
        # picker container and any other transient overlays.
        app = self.app
        action = getattr(app, "action_dismiss_overlay", None)
        if callable(action):
            action()


class AcornApp(App[None]):
    """Phase 5 shell."""

    CSS = """
    Screen { background: $surface; }
    #query_bar { height: 1; padding: 0 1; border: none; }
    #footer_hints { dock: bottom; height: 1; background: $surface; padding: 0 1; color: $text-muted; }
    /* Slim, lazygit-style scrollbars; horizontal scrolling disabled
       because long file names truncate cleanly and a 1-cell horizontal
       bar at the foot of every pane is just visual noise. */
    * { scrollbar-size-vertical: 1; scrollbar-size-horizontal: 1; }
    /* Pane borders dim by default, brighten when the pane (or any
       descendant) is focused — lazygit's active-section convention. */
    #results_column { width: 1fr; height: 1fr; }
    #results_pane {
        width: 100%; height: 2fr;
        border: round $primary 50%;
        overflow-x: hidden;
    }
    #results_pane:focus-within { border: round $accent; }
    #collections_panel_tree {
        width: 100%; height: 1fr;
        border: round $primary 50%;
        overflow-x: hidden;
    }
    #collections_panel_tree:focus-within { border: round $accent; }
    /* Section collapse-to-header: Left at the panel root shrinks the
       whole panel down to its border-title strip. */
    #results_pane.collapsed,
    #collections_panel_tree.collapsed { height: 3; }
    #preview_pane { width: 3fr; height: 1fr; border: round $primary 50%; padding: 0 1; }
    #preview_pane:focus-within { border: round $accent; }
    .preview-title { padding: 0 0 1 0; color: $accent; text-style: bold; }
    .chunk-section { padding: 0 0 1 0; height: auto; }
    .chunk-header { padding: 1 0 0 0; }
    .chunk-line { padding: 0 0 0 0; height: auto; }
    .chunk-line-match { background: $accent 8%; }
    .chunk-section-focused { background: $accent 15%; }
    #placeholder { color: $text-muted; }
    #help_overlay {
        layer: overlay;
        background: $panel;
        border: round $accent;
        margin: 2 4 3 4;
        padding: 1 2 2 2;
    }
    #cmd_palette {
        dock: bottom;
        height: 3;
        padding: 0 1;
        background: $panel;
    }
    #collection_picker {
        layer: overlay;
        background: $panel;
        border: round $accent;
        margin: 4 8;
        padding: 1 2 2 2;
        height: auto;
    }
    Tree > .tree--label { padding: 0 1; }
    /* Selected-row highlight: full-width accent (lazygit convention). */
    Tree > .tree--cursor { background: $accent 40%; color: $text; text-style: bold; }
    """

    # BINDINGS is built from the action registry at import time so footer
    # hints, help overlay, and runtime keymap all share one source.
    BINDINGS = [  # noqa: RUF012 — Textual's App.BINDINGS expects a class-level list literal
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("escape", "dismiss_overlay", "Close overlay", show=False),
        *(
            Binding(
                key,
                action_id,
                _short_label(action_id),
                show=_action_show(action_id),
            )
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
        config: Config | None = None,
    ) -> None:
        super().__init__()
        self._index_dir = index_dir or default_index_dir()
        # Active collections — list[] supports multi-select via the picker.
        # Empty list = "all collections" (no scope filter). When the user
        # didn't pass --collection, restore the last persisted scope so
        # the TUI starts where they left it.
        if collection:
            self._collections: list[str] = [collection]
            self._collapsed_panels: set[str] = set()
        else:
            from acorn.state import load as _load_state

            saved = _load_state()
            self._collections = list(saved.collections)
            self._collapsed_panels = set(saved.collapsed_panels)
        self._initial_query = initial_query
        self._searcher: Searcher | None = None
        self._current_query: str = ""
        self._groups: list[FileGroup] = []
        self._acorn_keymap = keymap or load_keymap()
        # Ranking profile applied at search time. Built from the active
        # collection's ``ranking_profile`` field; default profile (all-zero)
        # is the BM25 identity, so the no-config case is unchanged.
        self._config = config
        self._ranking_profile: RankingProfile = self._resolve_profile()
        # Last `:command` palette result, exposed for tests.
        self.last_palette_result: str | None = None
        # Cache of (parent_id) → list[FileChunk] so we don't re-fetch the
        # full document on every cursor move within the same file. Keyed by
        # parent_id, invalidated on new query.
        self._chunk_cache: dict[str, list[FileChunk]] = {}
        # Currently-rendered file's per-chunk header Static widgets, keyed
        # by chunk_seq. Cleared and rebuilt on each file change.
        self._chunk_widgets: dict[int, Static] = {}
        # First widget within each chunk that contains a query-term match.
        # When scrolling to a chunk we target this widget so the matched
        # text is visible, not just the chunk header. Falls back to the
        # chunk's header widget when there's no match in that chunk.
        self._match_targets: dict[int, Static] = {}
        # The parent_id whose chunks are currently mounted in the preview
        # pane (so we don't re-mount when cursor moves within the same file).
        self._preview_parent_id: str | None = None

    # ── Layout ────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Search…", id="query_bar", value=self._initial_query)
        with Horizontal():
            # Left column: results on top, collections panel below.
            with Vertical(id="results_column"):
                yield Tree("Results", id="results_pane")
                # Single-widget panel — its border_title carries the
                # "Collections — N/M active" header, matching the
                # results-pane styling.
                yield Tree("Collections", id="collections_panel_tree")
            # Preview pane: VerticalScroll containing one Static per chunk
            # so scroll_to_widget targets the exact chunk regardless of how
            # text wraps visually.
            with VerticalScroll(id="preview_pane"):
                yield Static("Type a query and press Enter.", id="placeholder")
        yield Static("", id="footer_hints")

    def on_mount(self) -> None:
        # Tokyo-night theme: muted blue/teal pastel palette per user request.
        self.theme = "tokyo-night"
        try:
            self._searcher = Searcher(index_dir=self._index_dir)
        except (FileNotFoundError, RuntimeError):
            # No index yet — the app still opens so the user can manage
            # collections, then reindex outside or from the CLI.
            self._searcher = None
        tree = self.query_one("#results_pane", Tree)
        tree.show_root = False
        tree.guide_depth = 2
        # Collections panel — populated from the loaded Config.
        ctree = self.query_one("#collections_panel_tree", Tree)
        ctree.show_root = False
        ctree.guide_depth = 2
        self._refresh_collections_panel()
        # Restore any panels the user collapsed-to-header in a previous
        # session.
        import contextlib

        for panel_id in self._collapsed_panels:
            with contextlib.suppress(Exception):
                self.query_one(f"#{panel_id}").add_class("collapsed")
        # Initial border titles — refreshed live as the user searches.
        self._refresh_status()
        if self._initial_query:
            self._run_query(self._initial_query)
        self.query_one("#query_bar", Input).focus()

    # ── Ranking profile (§7) ──────────────────────────────────────

    def _resolve_profile(self) -> RankingProfile:
        """Pick the ranking profile to apply to search results.

        Resolution order:
          1. If a single collection is active and its ``ranking_profile``
             is defined in the config, use that.
          2. Else fall back to the ``default`` ranking profile if defined.
          3. Else neutral (BM25 identity) — return ``RankingProfile()``.
        """
        if self._config is None:
            return RankingProfile()
        name = "default"
        if len(self._collections) == 1:
            try:
                col = self._config.collection(self._collections[0])
                name = col.ranking_profile or "default"
            except KeyError:
                name = "default"
        return profile_from_config(self._config.ranking_profile(name))

    # ── Pane border titles ────────────────────────────────────────

    def _results_title(self) -> str:
        """Border title for the results pane — counts live next to the data
        they describe, not in a global status bar."""
        n_files = len(self._groups)
        n_sections = sum(len(g.hits) for g in self._groups)
        if not self._groups:
            return "Results"
        return f"Results — {n_files} files / {n_sections} sections"

    def _preview_title(self) -> str:
        """Border title for the preview pane — file basename + chunk count
        for the document currently mounted there."""
        if self._preview_parent_id is None:
            return "Preview"
        for g in self._groups:
            if g.parent_id == self._preview_parent_id:
                return f"Preview — {Path(g.path).name}"
        return "Preview"

    def _refresh_status(self) -> None:
        try:
            self.query_one("#results_pane", Tree).border_title = self._results_title()
            self.query_one("#preview_pane").border_title = self._preview_title()
        except Exception:
            pass
        self._refresh_footer_hints()

    # ── Footer hints (focus-aware, lazygit-style) ─────────────────

    def _focus_context(self) -> str:
        """Resolve the current focus context for footer-hint filtering.

        Returns one of ``"query"``, ``"results"``, ``"preview"``, or
        ``"global"`` (when nothing app-relevant is focused, e.g. an
        overlay)."""
        focused = self.focused
        if focused is None:
            return "global"
        # Walk the ancestor chain looking for a recognisable id.
        node: Any | None = focused
        while node is not None:
            wid = getattr(node, "id", None)
            if wid == "query_bar":
                return "query"
            if wid == "results_pane":
                return "results"
            if wid == "collections_panel_tree":
                return "collections"
            if wid == "preview_pane":
                return "preview"
            node = getattr(node, "parent", None)
        return "global"

    def _refresh_footer_hints(self) -> None:
        """Rebuild the footer-hints Static text from REGISTRY, filtered
        by the current focus context. Capped at 6 hints — beyond that
        it stops being a glance."""
        ctx = self._focus_context()
        hints: list[str] = []
        for a in REGISTRY:
            if not a.show_in_footer:
                continue
            if a.contexts and ctx not in a.contexts:
                continue
            key = self._acorn_keymap.for_action(a.id)
            if not key:
                continue
            label = a.footer_label or _short_label(a.id)
            # Style the key glyph distinctly so the eye can scan keys
            # and labels separately. Rich uses ``[reverse]…[/]`` to set
            # an inverted background on the key portion.
            hints.append(f"[reverse] {_format_key_hint(key)} [/] {label}")
            if len(hints) >= 6:
                break
        import contextlib

        with contextlib.suppress(Exception):
            from rich.text import Text

            sep = Text("  │  ", style="dim")
            joined = Text("")
            for i, hint in enumerate(hints):
                if i:
                    joined.append_text(sep)
                joined.append_text(Text.from_markup(hint))
            self.query_one("#footer_hints", Static).update(joined)

    def on_descendant_focus(self) -> None:  # Textual fires this on focus changes
        self._refresh_footer_hints()

    # ── Search flow ───────────────────────────────────────────────

    @on(Input.Submitted, "#query_bar")
    def _on_query_submit(self, ev: Input.Submitted) -> None:
        self._run_query(ev.value)

    def _run_query(self, query: str) -> None:
        if self._searcher is None:
            return
        from acorn.filter_dsl import FilterError
        from acorn.query_dsl import split_metadata_filter

        # Extract a single inline [metadata filter] from the user query.
        # Bracket parse errors (unclosed, multiple) surface as a notify;
        # the search doesn't run.
        try:
            lexical, metadata_filter = split_metadata_filter(query)
        except ValueError as e:
            self.notify(str(e), severity="error", title="Filter syntax")
            self._groups = []
            self._refresh_results_tree()
            return

        self._current_query = query  # save the original (with [...]) for history
        # Multi-collection scoping: prefix with `c:a,b` so the DSL pre-pass
        # builds the correct (collection:"a" OR collection:"b") filter.
        if len(self._collections) >= 2:
            scoped_query = f"c:{','.join(self._collections)} {lexical}"
            single_col = None
        else:
            scoped_query = lexical
            single_col = self._collections[0] if self._collections else None
        try:
            self._groups = self._searcher.search_grouped(
                scoped_query,
                limit=50,
                sections_per_file=10,
                collection=single_col,
                profile=self._ranking_profile,
                metadata_filter=metadata_filter,
            )
        except FilterError as e:
            self.notify(
                f"col {e.column}: {e.message}",
                severity="error",
                title="Filter syntax",
            )
            self._groups = []
            self._refresh_results_tree()
            return
        # New query → invalidate the per-file chunk cache.
        self._chunk_cache.clear()
        self._refresh_results_tree()

    def _refresh_results_tree(self) -> None:
        """Rebuild the results tree from ``self._groups`` and refresh status.

        The top result is auto-expanded so its section rows (with their
        ``§ heading`` / ``p.N`` / ``chunk N`` locators) are immediately
        visible — saves a keypress and makes the locator format
        discoverable on first launch.
        """
        tree = self.query_one("#results_pane", Tree)
        tree.clear()
        max_score = max((g.top_score for g in self._groups), default=0.0)
        for i, g in enumerate(self._groups):
            file_node = tree.root.add(
                _format_file_label(g, max_score=max_score),
                data={"kind": "file", "group": g},
                expand=(i == 0),
            )
            for h in g.hits:
                file_node.add_leaf(
                    _format_hit_label(h, max_score=max_score),
                    data={"kind": "section", "hit": h},
                )
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
        """Render the full document for ``parent_id`` as one Static widget
        per chunk, then scroll the preview to the chunk identified by
        ``focus_chunk_seq``.

        Per-chunk widgets give precise ``scroll_to_widget`` targets,
        immune to long-line wrapping (which broke the previous
        line-offset approach for some PDFs)."""
        if self._searcher is None:
            return
        chunks = self._chunk_cache.get(parent_id)
        if chunks is None:
            chunks = self._searcher.get_file_chunks(parent_id)
            self._chunk_cache[parent_id] = chunks
        if not chunks:
            return

        # Mount fresh widgets only when the file has actually changed —
        # otherwise we just scroll within the existing widget tree.
        if parent_id != self._preview_parent_id:
            self._mount_chunks_for_file(parent_id, chunks)
            self._preview_parent_id = parent_id
            self._refresh_status()

        self._scroll_preview_to_chunk(focus_chunk_seq)

    def _mount_chunks_for_file(self, parent_id: str, chunks: list[FileChunk]) -> None:
        """Tear down the existing preview content and mount per-chunk widgets.

        Each chunk becomes a header Static plus one Static per non-empty body
        line. Per-line widgets give us a precise ``scroll_to_widget`` target
        for the FIRST matched line in a chunk, so the user lands on the
        highlighted match — not at the top of a 30-line page.
        """
        pane = self.query_one("#preview_pane", VerticalScroll)
        for w in list(pane.children):
            w.remove()
        self._chunk_widgets = {}
        self._match_targets = {}
        title = Static(Path(chunks[0].path).name, classes="preview-title")
        pane.mount(title)
        for c in chunks:
            header_text, pieces = render_chunk_pieces(c, query=self._current_query)
            header_w = Static(header_text, classes="chunk-section chunk-header")
            header_w.acorn_text = header_text  # type: ignore[attr-defined]
            pane.mount(header_w)
            self._chunk_widgets[c.chunk_seq] = header_w
            first_match: Static | None = None
            for line_text, has_match in pieces:
                line_w = Static(line_text, classes="chunk-line")
                line_w.acorn_text = line_text  # type: ignore[attr-defined]
                if has_match:
                    line_w.add_class("chunk-line-match")
                pane.mount(line_w)
                if has_match and first_match is None:
                    first_match = line_w
            self._match_targets[c.chunk_seq] = first_match or header_w

    def _scroll_preview_to_chunk(self, focus_chunk_seq: int) -> None:
        header = self._chunk_widgets.get(focus_chunk_seq)
        target = self._match_targets.get(focus_chunk_seq) or header
        if target is None:
            return
        # Highlight the focused chunk's header so the row is visually
        # unambiguous, even when we scroll to a match line below it.
        for w in self._chunk_widgets.values():
            w.remove_class("chunk-section-focused")
        if header is not None:
            header.add_class("chunk-section-focused")
        # Defer scroll until layout has settled (mount → reflow → measure).
        self.call_after_refresh(self._do_scroll_to_widget, target)

    def _do_scroll_to_widget(self, widget: Static) -> None:
        pane = self.query_one("#preview_pane", VerticalScroll)
        pane.scroll_to_widget(widget, top=True, animate=False)

    # ── Open / peek dispatch ──────────────────────────────────────

    # Note: we deliberately do NOT bind Tree.NodeSelected to opener.open_smart.
    # Per user feedback, clicking / Enter should populate the preview only;
    # opening externally requires the explicit `o` (open at locator) or `O`
    # (open default app) bindings. Selection still fires NodeHighlighted
    # which drives the preview render via `_on_tree_highlight`.

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

    def action_open_at_locator(self) -> None:
        """Open the focused result at its page/section.

        For PDFs with a non-empty query, routes through Skim's URL form
        so ``&search=`` highlights the term in the opened PDF (§22 Spike C).
        """
        tree = self.query_one("#results_pane", Tree)
        if tree.cursor_node is None:
            return
        target = self._target_for_node(tree.cursor_node)
        if target is None:
            return
        _, hit = target
        opener.open_smart(
            path=Path(hit.path),
            kind=hit.kind,
            page=hit.page,
            query=self._current_query,
        )

    def action_open_default_app(self) -> None:
        """Open the focused file in its default app, ignoring the locator."""
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

    def _focused_tree(self) -> Tree[Any] | None:
        """Whichever app-level tree currently owns focus, or None.

        Used by the smart-collapse / smart-expand actions so the same
        Left / Right semantics apply to every tree the user can focus
        (results pane and the collections panel today)."""
        ctx = self._focus_context()
        if ctx == "results":
            return self.query_one("#results_pane", Tree)
        if ctx == "collections":
            return self.query_one("#collections_panel_tree", Tree)
        return None

    def action_tree_smart_collapse(self) -> None:
        """Lazygit-style ``left``-arrow handling for any focused tree.

        Rules:
        - Panel already collapsed-to-header → no-op (re-expand via Right).
        - Leaf focused → collapse the parent, move cursor onto it.
        - Expanded branch focused → collapse it (cursor stays put).
        - Already-collapsed top-level node → collapse the whole panel
          to its header strip (lazygit's section-collapse gesture).
        - Already-collapsed branch with parent → walk up + collapse parent.

        Active for both the results tree and the collections panel; a
        no-op anywhere else.
        """
        tree = self._focused_tree()
        if tree is None:
            return
        if "collapsed" in tree.classes:
            return
        node = tree.cursor_node
        if node is None:
            return
        if not node.children or not node.is_expanded:
            parent = node.parent
            # tree.root is a hidden virtual root; treat it as "no parent".
            if parent is None or parent is tree.root:
                # Top of the tree, already collapsed — collapse the
                # entire panel to its header strip.
                if tree.id:
                    tree.add_class("collapsed")
                    self._collapsed_panels.add(tree.id)
                    self._persist_state()
                return
            parent.collapse()
            tree.move_cursor(parent)
            return
        node.collapse()

    def action_tree_smart_expand(self) -> None:
        """Right-arrow companion to ``action_tree_smart_collapse``.

        - Panel collapsed-to-header → re-expand the panel.
        - Collapsed branch with children → expand it.
        - Already-expanded branch → move cursor to its first child.
        - Leaf / no children → no-op.
        """
        tree = self._focused_tree()
        if tree is None:
            return
        if "collapsed" in tree.classes:
            tree.remove_class("collapsed")
            if tree.id:
                self._collapsed_panels.discard(tree.id)
                self._persist_state()
            return
        node = tree.cursor_node
        if node is None or not node.children:
            return
        if not node.is_expanded:
            node.expand()
            return
        first_child = node.children[0]
        tree.move_cursor(first_child)

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
        sel_list = _PickerSelectionList(*selections, id="collection_selection")
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
        # Single-collection scopes can pull a per-collection ranking profile;
        # multi-scopes fall back to the default profile.
        self._ranking_profile = self._resolve_profile()
        self._refresh_status()
        self._refresh_collections_panel()
        self._persist_state()

    def _persist_state(self) -> None:
        """Save the current scope + panel state to disk so the next
        launch starts where the user left off."""
        from acorn.state import UiState, save

        save(
            UiState(
                collections=list(self._collections),
                sources=[],
                collapsed_panels=sorted(self._collapsed_panels),
            )
        )

    # ── Collections panel (UX-D) ─────────────────────────────────

    def _refresh_collections_panel(self) -> None:
        """Repopulate the lazygit-style collections panel from the loaded
        Config, marking the currently-active collections."""
        try:
            tree = self.query_one("#collections_panel_tree", Tree)
        except Exception:
            return
        cfg = self._config
        if cfg is None:
            from acorn.config import load as load_config

            try:
                cfg = load_config()
            except Exception:
                cfg = None
        names = sorted(cfg.collections.keys()) if cfg else []
        active = set(self._collections)
        tree.show_root = False
        tree.clear()
        for name in names:
            col = cfg.collections[name] if cfg else None
            marker = "●" if name in active else "○"
            n_sources = len(col.sources) if col else 0
            label = f"{marker}  {name}  ({n_sources} source{'s' if n_sources != 1 else ''})"
            node = tree.root.add(label, data={"kind": "collection", "name": name}, expand=False)
            if col:
                for i, s in enumerate(col.sources):
                    # Show only the path's basename — full paths blow out
                    # the panel width and trigger horizontal overflow.
                    short = Path(str(s.path)).name or str(s.path)
                    src_label = f"{i + 1}. {short}"
                    node.add_leaf(src_label, data={"kind": "source", "collection": name})
        tree.border_title = f"Collections — {len(active)}/{len(names)} active"

    @on(Tree.NodeSelected, "#collections_panel_tree")
    def _on_collections_panel_selected(self, ev: Tree.NodeSelected[dict[str, object]]) -> None:
        """Enter on a tree node toggles the parent collection's
        membership in the active scope (per the user's explicit
        request: Enter, not Space). Source nodes route to their parent
        collection — no per-source scoping yet."""
        data = ev.node.data or {}
        kind = data.get("kind")
        if kind not in {"collection", "source"}:
            return
        name = str(data.get("name") if kind == "collection" else data.get("collection") or "")
        if not name:
            return
        if name in self._collections:
            self._collections.remove(name)
        else:
            self._collections.append(name)
        self._ranking_profile = self._resolve_profile()
        self._refresh_collections_panel()
        self._refresh_status()
        self._persist_state()
        # Re-run the current query against the new scope so the user sees
        # the effect immediately.
        if self._current_query:
            self._run_query(self._current_query)

    def action_dismiss_overlay(self) -> None:
        """Close any open overlay (help, picker, palette). No-op if none."""
        for selector in ("#help_overlay", "#collection_picker", "#cmd_palette"):
            for w in self.query(selector):
                w.remove()

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

    def action_open_collections_form(self) -> None:
        """Push the Collections screen for browsing / editing collections."""
        from acorn.config import default_config_path
        from acorn.tui.collections_screen import CollectionsScreen

        # Use the config that was loaded at TUI launch as the starting point;
        # the screen will reload from disk before showing to pick up any
        # external edits.
        if self._config is None:
            return
        screen = CollectionsScreen(self._config, config_path=default_config_path())
        self.push_screen(screen, callback=self._on_collections_form_dismissed)

    def _on_collections_form_dismissed(self, _result: object) -> None:
        """The form may have written changes to disk; reload our cached
        Config so subsequent searches use the new collection set."""
        from acorn.config import load

        self._config = load()
        # Recompute ranking profile in case the active collection's profile
        # was edited.
        self._ranking_profile = self._resolve_profile()
        self._refresh_status()

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

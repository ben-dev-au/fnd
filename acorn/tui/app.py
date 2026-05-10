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

import re
from collections import OrderedDict
from pathlib import Path
from typing import Any

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import (
    Input,
    Label,
    ProgressBar,
    SelectionList,
    Static,
    Tree,
)
from textual.widgets.selection_list import Selection
from textual.widgets.tree import TreeNode

from acorn import opener
from acorn.config import Config, default_index_dir
from acorn.explain import SearchTrace
from acorn.query import FileChunk, FileGroup, Hit, Searcher
from acorn.render import render_chunk_pieces
from acorn.rerank import RankingProfile, profile_from_config
from acorn.tui.actions import REGISTRY, Keymap, load_keymap, resolve_command
from acorn.tui.preview_scrollbar import MatchAwareScroll

_PASS_GLYPHS = {0: "●", 1: "~", 2: "⊕", 3: "❝"}


# Preview widget cache (UX-pass-4 §4 hybrid follow-up).
#
# Repeat visits to a previously-loaded file should be instant. The decode
# cache (_chunk_cache) saved the chunk DATA but mounting hundreds of
# rich-rendered widgets each visit was the actual bottleneck. Keep the
# mounted widget tree alive in a per-file Container; switching files is
# then a single class-toggle. Bounded by LRU + a chunk-count threshold so
# small files (which mount instantly anyway) don't bloat memory.
_PREVIEW_CACHE_MAX_FILES = 8
_PREVIEW_CACHE_MIN_CHUNKS = 30
# Visible-first mount window — chunks are decoded already, mounting
# focused ± these counts synchronously gives the user instant viewport
# feedback before the background fill starts.
_VISIBLE_FIRST_ABOVE = 7
_VISIBLE_FIRST_BELOW = 7


class PreviewContainer(Container):
    """Per-file preview container holding the mounted chunk widgets.

    One container per cached file lives inside ``#preview_pane``; only
    one is ``-active`` (visible) at a time. Switching files toggles
    classes — no remount cost. Each container tracks which chunk
    indices have been mounted so a partial-then-cancelled mount can be
    resumed on revisit.
    """

    DEFAULT_CSS = """
    PreviewContainer { width: 100%; height: auto; }
    PreviewContainer.-hidden { display: none; }
    """

    def __init__(
        self,
        *,
        parent_doc_id: str,
        query_signature: str,
        total_chunks: int,
    ) -> None:
        super().__init__()
        self.parent_doc_id = parent_doc_id
        self.query_signature = query_signature
        self.total_chunks = total_chunks
        self.mounted_indices: set[int] = set()
        # chunk_seq → first widget for that chunk (the header / title row).
        self.chunk_widgets: dict[int, Static] = {}
        # chunk_seq → first match-bearing widget (or header when no match).
        self.match_targets: dict[int, Static] = {}

    @property
    def is_complete(self) -> bool:
        return len(self.mounted_indices) >= self.total_chunks


class PreviewCache:
    """LRU cache of :class:`PreviewContainer`, keyed by
    ``(parent_doc_id, query_signature)``. Files with fewer than
    :data:`_PREVIEW_CACHE_MIN_CHUNKS` chunks are NOT cached — they
    mount fast enough that keeping the widget tree alive isn't worth
    the memory.
    """

    def __init__(
        self,
        *,
        max_files: int = _PREVIEW_CACHE_MAX_FILES,
        min_chunks: int = _PREVIEW_CACHE_MIN_CHUNKS,
    ) -> None:
        self._cache: OrderedDict[tuple[str, str], PreviewContainer] = OrderedDict()
        self.max_files = max_files
        self.min_chunks = min_chunks

    def get(self, parent_doc_id: str, query_signature: str) -> PreviewContainer | None:
        key = (parent_doc_id, query_signature)
        container = self._cache.get(key)
        if container is not None:
            self._cache.move_to_end(key)
        return container

    def put(self, container: PreviewContainer) -> list[PreviewContainer]:
        """Cache ``container`` (only if it meets the size threshold) and
        return any LRU-evicted containers the caller must remove from the
        DOM."""
        if container.total_chunks < self.min_chunks:
            return []
        key = (container.parent_doc_id, container.query_signature)
        self._cache[key] = container
        self._cache.move_to_end(key)
        evicted: list[PreviewContainer] = []
        while len(self._cache) > self.max_files:
            _, old = self._cache.popitem(last=False)
            evicted.append(old)
        return evicted

    def clear(self) -> list[PreviewContainer]:
        """Drop everything; return the previously-cached containers so
        the caller can remove them from the DOM."""
        evicted = list(self._cache.values())
        self._cache.clear()
        return evicted


# Phase F filters: panel layout. ``kinds`` is multi-select (each value
# toggles independently); ``date`` is a radio (single-select; selecting
# a new value replaces the previous). The presentation labels live next
# to the values so the panel renders without further lookup tables.
_FILTER_KINDS: tuple[str, ...] = ("pdf", "docx", "pptx", "md", "txt")
_FILTER_DATES: tuple[str, ...] = ("any", "today", "week", "month", "year")


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


_NON_WORD_RE = re.compile(r"[^a-z0-9]+")


def _trim_redundant_heading(heading_path: str, title: str, path: str) -> str:
    """Strip leading segments that just repeat words from the filename or
    title. The result tree's parent row already shows the filename, so
    prefixing every section row with the same words is just clutter.

    A leading ``Templates`` segment is dropped when ``Templates`` also
    appears as a word in the file basename (``DPC Wk8 Notes - Templates,
    Strategy Pattern & C++ Streams``) or in the title — covers both the
    pure ``# Templates`` H1 case and the deep multi-word filename case.
    """
    if not heading_path:
        return ""

    def _words(s: str) -> set[str]:
        return {w for w in _NON_WORD_RE.split(s.lower()) if w}

    parts = [p.strip() for p in heading_path.split(">") if p.strip()]
    haystack = _words(Path(path).stem) | _words(title or "")
    # If every segment is just a word from the filename / title, the
    # whole crumb is redundant — keep the deepest one as the location
    # marker, or drop it entirely when there's only one segment so the
    # caller can fall back to a chunk locator.
    if parts and all(_words(p).issubset(haystack) for p in parts):
        return parts[-1] if len(parts) > 1 else ""
    while parts and _words(parts[0]).issubset(haystack):
        parts.pop(0)
    return " > ".join(parts)


def _shorten(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` chars with an ellipsis suffix."""
    text = text.strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _format_hit_label(h: Hit, *, max_score: float = 0.0) -> Any:
    """Result-tree row label: short locator left, snippet right.

    Locator is a few chars (page / slide / trimmed heading / chunk N)
    so the body snippet — the actually useful context for "is this
    the match I want" — claims most of the row width.
    """
    if h.page_label:
        loc = f"p.{h.page_label}"
    elif h.page:
        loc = f"p.{h.page}"
    elif h.slide:
        loc = f"s.{h.slide}"
    else:
        trimmed = _trim_redundant_heading(h.heading_path, h.title, h.path)
        loc = _shorten(trimmed, 18) if trimmed else f"§{h.chunk_seq + 1}"
    snippet = _shorten(h.snippet, 80) if h.snippet else ""
    body = f"{loc}  {snippet}" if snippet else loc
    glyph = _PASS_GLYPHS.get(h.pass_index, "")
    pass_marker = f" {glyph}" if h.pass_index > 0 else ""
    return _build_label(f"{body}{pass_marker}", h.score, max_score)


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


class _PrefixingSearcher:
    """Wrap a :class:`Searcher` and AND a fixed filter prefix into every
    query string before it reaches Tantivy.

    Fusion's phrase pass would otherwise wrap the whole query (including
    field-restrictor prefixes like ``kind:md``) in quotes, which the
    Tantivy parser reads as a literal phrase. By keeping the lexical
    part clean and re-attaching the filter prefix at every sub-query
    issue point, both fusion and cascade get correct field-restricted
    behaviour without changing their public signatures.
    """

    def __init__(self, inner: Searcher, *, prefix: str) -> None:
        self._inner = inner
        self._prefix = prefix.strip()

    def _wrap(self, query: str) -> str:
        if not self._prefix:
            return query
        return f"({self._prefix}) AND ({query})"

    def _filtered_raw_hits(self, query: str, **kwargs: Any) -> list[Hit]:
        return self._inner._filtered_raw_hits(self._wrap(query), **kwargs)

    def _raw_hits(self, query: str, **kwargs: Any) -> list[Hit]:
        return self._inner._raw_hits(self._wrap(query), **kwargs)

    def __getattr__(self, name: str) -> Any:
        # Forward attribute access to the underlying searcher (e.g.
        # ``_searcher`` for fuzzy_pass's typed-API path, plus public
        # methods callers might still want).
        return getattr(self._inner, name)


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
    /* Lazygit-thin scrollbars: 1 cell wide, fully transparent track so
       only the thumb glyph shows against the screen background. The
       custom MatchAwareScrollBarRender (preview only) overlays accent
       markers on the track at chunk-match positions. */
    * {
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
        scrollbar-background: transparent;
        scrollbar-background-active: transparent;
        scrollbar-background-hover: transparent;
        scrollbar-color: $primary 60%;
        scrollbar-color-active: $accent;
        scrollbar-color-hover: $accent 70%;
        scrollbar-corner-color: transparent;
    }
    /* Pane borders dim by default, brighten when the pane (or any
       descendant) is focused — lazygit's active-section convention.
       Results expands to fill remaining space; Collections + Filters
       size to their content so each panel stays exactly as tall as
       its rows demand. */
    #results_column { width: 1fr; height: 1fr; }
    #results_pane {
        width: 100%; height: 1fr;
        border: round $primary 50%;
        overflow-x: hidden;
    }
    #results_pane:focus-within { border: round $accent; }
    #collections_panel_tree {
        width: 100%; height: auto;
        max-height: 50%;
        border: round $primary 50%;
        overflow-x: hidden;
    }
    #collections_panel_tree:focus-within { border: round $accent; }
    #filters_panel_tree {
        width: 100%; height: auto;
        max-height: 50%;
        border: round $primary 50%;
        overflow-x: hidden;
    }
    #filters_panel_tree:focus-within { border: round $accent; }
    /* Section collapse-to-header: Left at the panel root shrinks the
       whole panel down to its border-title strip. ``overflow: hidden``
       suppresses any rogue scrollbar that would otherwise sneak past
       the 3-cell height when the inner tree has more rows than fit. */
    #results_pane.collapsed,
    #collections_panel_tree.collapsed,
    #filters_panel_tree.collapsed {
        height: 3;
        overflow: hidden;
        scrollbar-size-vertical: 0;
        scrollbar-size-horizontal: 0;
    }
    /* Right-side preview column: always-on ProgressBar above, scrollable
       preview pane below. Column width matches what #preview_pane used
       to claim directly. */
    #preview_column { width: 2fr; height: 1fr; }
    #preview_pane {
        width: 100%; height: 1fr;
        border: round $primary 50%;
        padding: 0 0 0 1;
    }
    #preview_pane:focus-within { border: round $accent; }
    /* While a partial mount is in flight we hide the scrollbar (its
       virtual size keeps growing as chunks land, so the thumb would
       jitter). Programmatic ``scroll_to_widget`` calls during phase 2b
       still need to work — using ``overflow-y: hidden`` would prevent
       that, so we only suppress the bar's chrome, not scrolling. */
    #preview_pane.is-loading { scrollbar-size-vertical: 0; }
    /* Preview-load progress bar — sibling of the pane (NOT inside it),
       always present, hidden via class. Show/hide is a single class
       toggle, no DOM mount races. */
    .preview-progress {
        width: 100%;
        height: 1;
        margin: 0 1 0 1;
        background: transparent;
    }
    .preview-progress.-hidden { display: none; }
    .preview-progress Bar { width: 1fr; color: $accent; }
    .preview-progress PercentageStatus { color: $text-muted; padding: 0 0 0 1; }
    .preview-title { padding: 0 0 1 0; color: $accent; text-style: bold; }
    .chunk-section { padding: 0 0 1 0; height: auto; }
    .chunk-line { padding: 0 0 0 0; height: auto; }
    .chunk-line-match { background: $accent 8%; }
    .chunk-section-focused { background: $accent 15%; }
    /* First widget of each chunk gets a single-row top gap so the
       reader can still see chunk boundaries without the locator
       header text (which lives in the sidebar). */
    .chunk-first { padding: 1 0 0 0; }
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
    /* Selected-row highlight: only when the panel actually owns focus
       — sidebar panels stop showing a stale ``current selection`` band
       when the user is typing in the query bar or has focus elsewhere. */
    Tree > .tree--cursor { background: transparent; color: $text; }
    Tree:focus-within > .tree--cursor { background: $accent 40%; text-style: bold; }
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
            self._active_sources: list[str] = []
            self._filter_kinds: list[str] = []
            self._filter_date: str = "any"
        else:
            from acorn.state import load as _load_state

            saved = _load_state()
            self._collections = list(saved.collections)
            self._collapsed_panels = set(saved.collapsed_panels)
            self._active_sources = list(saved.sources)
            self._filter_kinds = list(saved.filter_kinds)
            self._filter_date = saved.filter_date or "any"
        self._initial_query = initial_query
        self._searcher: Searcher | None = None
        self._current_query: str = ""
        # Last :multi block's intent line, if any. Disables strong-signal
        # bypass and biases snippet selection (UX-pass-4 §3). None until
        # the user submits a :multi block.
        self._current_intent: str | None = None
        self._groups: list[FileGroup] = []
        # Most-recent SearchTrace, populated on every _run_query so the
        # :explain overlay (UX-pass-4 §2) can dump it as JSON. None until
        # the first search runs.
        self._latest_trace: SearchTrace | None = None
        self._acorn_keymap = keymap or load_keymap()
        # Synonyms for §9c cascade and §9d fusion's ``syn`` sub-query.
        # Missing file → empty table → no synonym expansion (no-op).
        from acorn.config import app_data_dir
        from acorn.synonyms import SynonymTable, load_synonyms

        try:
            self._synonyms: SynonymTable = load_synonyms(app_data_dir() / "synonyms.toml")
        except Exception:
            self._synonyms = SynonymTable()
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
        # Widget-level cache (UX-pass-4 §4 hybrid): keeps the mounted
        # widget tree alive across file switches so repeat visits are
        # O(1). Cleared on every new query (highlights would be wrong).
        self._preview_cache: PreviewCache = PreviewCache()
        # The currently-active PreviewContainer (the one with `-active`
        # class). None until the first file is rendered.
        self._active_preview: PreviewContainer | None = None
        # Convenience aliases that point into the active container —
        # legacy code paths (_scroll_preview_to_chunk, etc.) read from
        # these instead of poking at the container directly.
        self._chunk_widgets: dict[int, Static] = {}
        self._match_targets: dict[int, Static] = {}
        # The parent_id whose chunks are currently mounted in the preview
        # pane (so we don't re-mount when cursor moves within the same file).
        self._preview_parent_id: str | None = None
        # Preview-load progress: ``(loaded, total)`` while a chunk-decode +
        # mount worker is running, ``None`` otherwise (UX-pass-4 §4).
        # ``total`` is ``None`` during the indeterminate decode phase (the
        # ProgressBar widget at the top of the pane carries the visible
        # signal); switches to the actual count once chunks arrive.
        self._preview_load_progress: tuple[int, int | None] | None = None
        # Strong reference to the in-flight preview-mount task so the
        # event loop doesn't garbage-collect it mid-run (asyncio task
        # refs are weak — GC == cancellation).
        self._preview_mount_task: object | None = None

    # ── Layout ────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Search…", id="query_bar", value=self._initial_query)
        with Horizontal():
            # Left column: results on top, then Collections, then Filters.
            with Vertical(id="results_column"):
                yield Tree("Results", id="results_pane")
                # Single-widget panels — each carries its summary in
                # ``border_title``, matching the results-pane styling.
                yield Tree("Collections", id="collections_panel_tree")
                yield Tree("Filters", id="filters_panel_tree")
            # Right column: always-on ProgressBar above the preview pane
            # (UX-pass-4 §4 follow-up). Keeping the bar OUTSIDE the
            # scrollable pane means its visibility is a single class
            # toggle — no mount/remove DOM races, no waiting on
            # remove_children to drain. Bar starts hidden.
            with Vertical(id="preview_column"):
                yield ProgressBar(
                    total=None,
                    show_eta=False,
                    show_percentage=True,
                    classes="preview-progress -hidden",
                    id="preview_progress_bar",
                )
                # Preview pane: MatchAwareScroll = VerticalScroll with a
                # custom scrollbar that overlays chunk-match markers on
                # the track. Per-file Containers hold the actual chunk
                # widgets so cache hits are O(1) display flips.
                with MatchAwareScroll(id="preview_pane"):
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
        # Filters panel — kind/date selectors that compose into the query.
        ftree = self.query_one("#filters_panel_tree", Tree)
        ftree.show_root = False
        ftree.guide_depth = 2
        self._refresh_filters_panel()
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
        """Border title for the preview pane — file basename only.

        Load progress lives on the ProgressBar widget mounted at the
        top of the pane (UX-pass-4 §4 follow-up); the title stays clean
        and stable so long filenames don't push state markers off the
        right edge.
        """
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
            if wid == "filters_panel_tree":
                return "filters"
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
        # Phase F: build the filter scaffolding (kind:, mtime:) and
        # multi-collection scope (c:) as a SEPARATE prefix. The lexical
        # part stays clean so the §9d fusion phrase-pass can wrap it
        # in quotes without dragging field qualifiers inside the
        # phrase (which Tantivy would parse as a literal phrase
        # ``kind:md glimmer`` rather than a field-restricted query).
        filter_clauses: list[str] = []
        if self._filter_kinds:
            if len(self._filter_kinds) == 1:
                filter_clauses.append(f"kind:{self._filter_kinds[0]}")
            else:
                filter_clauses.append(f"kind:({' '.join(sorted(self._filter_kinds))})")
        if self._filter_date and self._filter_date != "any":
            filter_clauses.append(f"mtime:{self._filter_date}")
        if len(self._collections) >= 2:
            filter_clauses.append(f"c:{','.join(self._collections)}")
            single_col = None
        else:
            single_col = self._collections[0] if self._collections else None
        filter_prefix = " ".join(filter_clauses)
        try:
            self._groups = self._search_layered(
                lexical=lexical,
                filter_prefix=filter_prefix,
                limit=50,
                sections_per_file=10,
                collection=single_col,
                metadata_filter=metadata_filter,
                active_sources=list(self._active_sources) or None,
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
        # New query → invalidate BOTH caches:
        # * _chunk_cache (decoded chunk data; rebuilt by next decode)
        # * _preview_cache (mounted widgets; their highlights were baked
        #   from the previous query, so they're stale even if the file
        #   shows up in the new results)
        # The cache invalidation also drops the rendered widgets from
        # the DOM so the next preview load starts from a clean slate.
        import contextlib

        self._chunk_cache.clear()
        self._cancel_preview_mount_task()
        evicted = self._preview_cache.clear()
        for old in evicted:
            with contextlib.suppress(Exception):
                old.remove()
        # Also drop the currently-active container if any (it was
        # already evicted above if it was in cache; otherwise it's a
        # small file that wasn't cached and we still need to clear).
        if self._active_preview is not None and self._active_preview.parent is not None:
            with contextlib.suppress(Exception):
                self._active_preview.remove()
        self._active_preview = None
        self._chunk_widgets = {}
        self._match_targets = {}
        self._preview_parent_id = None
        self._hide_progress_bar()
        self._refresh_results_tree()

    def _search_layered(
        self,
        *,
        lexical: str,
        filter_prefix: str,
        limit: int,
        sections_per_file: int,
        collection: str | None,
        metadata_filter: str | None,
        active_sources: list[str] | None,
    ) -> list[FileGroup]:
        """Master plan §9c + §9d wiring + UX-pass-4 §1 strong-signal regime.

        Delegates the regime decision to :func:`acorn.layered.search_layered`
        so the TUI and CLI share one entry point. ``filter_prefix`` is
        applied via :class:`_PrefixingSearcher` so fusion + cascade +
        the regime probe all see the same effective query without any
        signature changes.
        """
        if self._searcher is None or not lexical.strip():
            self._latest_trace = None
            return []
        from acorn.layered import search_layered

        searcher = (
            _PrefixingSearcher(self._searcher, prefix=filter_prefix)
            if filter_prefix
            else self._searcher
        )
        groups, trace = search_layered(
            searcher,  # type: ignore[arg-type]
            query=lexical,
            limit=limit,
            sections_per_file=sections_per_file,
            collection=collection,
            synonyms=self._synonyms,
            metadata_filter=metadata_filter,
            active_sources=active_sources,
            intent=self._current_intent,
            profile=self._ranking_profile,
            with_trace=True,
        )
        self._latest_trace = trace
        return groups

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
        """Render the full document for ``parent_id`` as one widget per
        chunk, then scroll to the chunk identified by ``focus_chunk_seq``.

        Hybrid load (UX-pass-4 §4 follow-up):

        1. Look up the cached :class:`PreviewContainer` for this file +
           query. If complete, activate (O(1) class flip) and scroll —
           done.
        2. If partial (resume case), activate, scroll, dispatch a task
           that mounts only the un-mounted indices.
        3. If absent and the chunk DATA is cached, create a fresh
           container and dispatch the visible-first + background mount.
        4. If even the chunk data is missing, dispatch the decode worker
           first; its callback enters step 3.
        """
        import asyncio

        if self._searcher is None:
            return

        chunks = self._chunk_cache.get(parent_id)
        if chunks is not None:
            # We have decoded data — go to the mount path.
            self._dispatch_preview_mount(parent_id, focus_chunk_seq, chunks)
            return

        # Need to decode first. The bar appears immediately; the worker
        # decodes off-thread and its callback re-enters via the chunk
        # data path.
        self._cancel_preview_mount_task()
        self._show_progress_bar(total=None)

        target_parent_id = parent_id
        target_focus = focus_chunk_seq
        searcher = self._searcher
        app = self

        def _load() -> None:
            try:
                fetched = searcher.get_file_chunks(target_parent_id)
            except Exception as e:
                app.call_from_thread(app._on_preview_load_failed, e)
                return
            app.call_from_thread(
                app._on_preview_chunks_loaded,
                target_parent_id,
                target_focus,
                fetched,
            )

        _ = asyncio.get_event_loop()  # ensure a loop exists for the callback
        self.run_worker(_load, thread=True, exclusive=True, group="preview-load")

    def _dispatch_preview_mount(
        self,
        parent_id: str,
        focus_chunk_seq: int,
        chunks: list[FileChunk],
    ) -> None:
        """Decide which mount path to take given the (parent_id, query)
        cache state. Always synchronous in its decision so the bar can
        appear instantly when there's actual work to do."""
        import asyncio

        query_sig = self._current_query_signature()

        # Same file + same query already active. Two sub-cases:
        #   (a) target chunk widget exists — just scroll, no remount.
        #   (b) target chunk not yet mounted (still loading the file
        #       and the user clicked a result above the load front):
        #       cancel the in-flight task and resume the SAME container
        #       with the new focus window. We keep all already-mounted
        #       chunks; the worker only mounts the missing ones.
        if (
            self._active_preview is not None
            and self._active_preview.parent_doc_id == parent_id
            and self._active_preview.query_signature == query_sig
        ):
            container = self._active_preview
            if container.is_complete or focus_chunk_seq in container.chunk_widgets:
                self._scroll_preview_to_chunk(focus_chunk_seq)
                return
            self._cancel_preview_mount_task()
            self._show_progress_bar(
                total=len(chunks),
                progress=len(container.mounted_indices),
            )
            self._preview_mount_task = asyncio.create_task(
                self._mount_chunks_async(parent_id, focus_chunk_seq, chunks, container)
            )
            return

        self._cancel_preview_mount_task()
        cached = self._preview_cache.get(parent_id, query_sig)

        if cached is not None and cached.is_complete:
            # Instant return — flip the active container, scroll, hide bar.
            # The match-scrollbar marker map is per-file, so a cache hit
            # still has to refresh it; otherwise the previous file's map
            # bleeds through and shows phantom marks across the whole bar.
            self._activate_preview_container(cached)
            self._refresh_match_scrollbar(chunks)
            self._scroll_preview_to_chunk(focus_chunk_seq)
            self._hide_progress_bar()
            return

        # Either no container yet OR a partially-mounted one (resume).
        # Either way, kick off the mount task; show the bar immediately
        # so the user sees feedback before the task even starts.
        if cached is None:
            container = PreviewContainer(
                parent_doc_id=parent_id,
                query_signature=query_sig,
                total_chunks=len(chunks),
            )
        else:
            container = cached
        self._show_progress_bar(
            total=len(chunks),
            progress=len(container.mounted_indices),
        )
        self._preview_mount_task = asyncio.create_task(
            self._mount_chunks_async(parent_id, focus_chunk_seq, chunks, container)
        )

    def _current_query_signature(self) -> str:
        """Stable signature for the current query — match-bearing
        widgets are baked with this query's highlights, so the cache
        must invalidate when it changes. Includes intent because intent
        biases snippet selection (UX-pass-4 §3)."""
        return f"{self._current_query}|{self._current_intent or ''}"

    def _show_progress_bar(self, *, total: int | None, progress: int = 0) -> None:
        """Show the always-on ProgressBar at the top of the preview
        column. ``total=None`` switches it to indeterminate (animated)
        mode for the decode phase; a real total switches to determinate
        with the percentage label visible."""
        import contextlib

        try:
            bar = self.query_one("#preview_progress_bar", ProgressBar)
        except Exception:
            return
        with contextlib.suppress(Exception):
            bar.update(total=total, progress=progress)
        bar.remove_class("-hidden")
        # Lock pane scrolling while a mount is in flight; the bar +
        # match-scrollbar markers tell the user where things are
        # without them needing to scroll.
        try:
            pane = self.query_one("#preview_pane", VerticalScroll)
            pane.add_class("is-loading")
        except Exception:
            pass

    def _hide_progress_bar(self) -> None:
        """Hide the always-on ProgressBar and re-enable pane scrolling."""
        import contextlib

        try:
            bar = self.query_one("#preview_progress_bar", ProgressBar)
        except Exception:
            return
        bar.add_class("-hidden")
        with contextlib.suppress(Exception):
            bar.update(total=None, progress=0)
        try:
            pane = self.query_one("#preview_pane", VerticalScroll)
            pane.remove_class("is-loading")
        except Exception:
            pass

    def _update_progress_bar(self, progress: int) -> None:
        """Bump the always-on ProgressBar's current progress."""
        import contextlib

        try:
            bar = self.query_one("#preview_progress_bar", ProgressBar)
        except Exception:
            return
        with contextlib.suppress(Exception):
            bar.update(progress=progress)

    def _activate_preview_container(self, container: PreviewContainer) -> None:
        """Make ``container`` the visible preview; hide all others."""
        for child in self.query(PreviewContainer):
            if child is container:
                child.remove_class("-hidden")
            else:
                child.add_class("-hidden")
        self._active_preview = container
        self._preview_parent_id = container.parent_doc_id
        # Update the legacy aliases that other code paths read from.
        self._chunk_widgets = container.chunk_widgets
        self._match_targets = container.match_targets

    def _cancel_preview_mount_task(self) -> None:
        """Cancel any in-flight mount task. The cancelled task's
        partial-mount state lives on its :class:`PreviewContainer`,
        so a later visit can resume it."""
        import contextlib

        task = self._preview_mount_task
        if task is None:
            return
        try:
            done = task.done()  # type: ignore[attr-defined]
        except Exception:
            done = True
        if not done:
            with contextlib.suppress(Exception):
                task.cancel()  # type: ignore[attr-defined]
        self._preview_mount_task = None

    def _on_preview_load_failed(self, exc: BaseException) -> None:
        """Worker error callback. Hide the bar, surface a notify."""
        self._hide_progress_bar()
        self.notify(f"Preview load failed: {exc}", severity="error")

    def _on_preview_chunks_loaded(
        self,
        parent_id: str,
        focus_chunk_seq: int,
        chunks: list[FileChunk],
    ) -> None:
        """Worker callback. Caches chunk data; hands off to the same
        mount path the cache-hit case uses."""
        self._chunk_cache[parent_id] = chunks
        if not chunks:
            # Empty file — hide bar, leave pane blank.
            self._hide_progress_bar()
            self._preview_parent_id = parent_id
            self._refresh_status()
            return
        self._dispatch_preview_mount(parent_id, focus_chunk_seq, chunks)

    async def _mount_chunks_async(
        self,
        parent_id: str,
        focus_chunk_seq: int,
        chunks: list[FileChunk],
        container: PreviewContainer,
    ) -> None:
        """Visible-first mount + hidden-prepend background fill.

        Phase 1 (sync, fast): mount the focused chunk plus
        :data:`_VISIBLE_FIRST_ABOVE` chunks above and
        :data:`_VISIBLE_FIRST_BELOW` below — a window roughly matching
        the typical viewport. The user sees the relevant content
        instantly.

        Phase 2a (async, batched): append the remaining chunks BELOW
        the visible window in document order. These add to virtual
        size but don't shift the visible viewport.

        Phase 2b (async, batched): mount the chunks ABOVE the visible
        window, but set ``display = False`` on each newly-mounted
        widget the moment it lands. Hidden widgets contribute zero
        layout, so the focused chunk's screen position stays put while
        the background fill runs (no jumping). After the last above-
        window chunk is mounted, we reveal the entire batch at once
        and re-anchor scroll to the focused chunk's top edge.

        Cancellation is non-destructive: partial state lives on
        ``container.mounted_indices``. The ``finally`` block always
        reveals any still-hidden widgets so a cancelled task doesn't
        leave the container in a half-hidden state.
        """
        import asyncio
        import contextlib

        pane = self.query_one("#preview_pane", VerticalScroll)

        # Make sure container is mounted in the pane and active.
        if container.parent is None:
            await pane.remove_children(".placeholder")
            pane.mount(container)
        self._activate_preview_container(container)
        self._refresh_match_scrollbar(chunks)

        # Establish the focused window indices (clamped to chunks).
        focus_idx = next(
            (i for i, c in enumerate(chunks) if c.chunk_seq == focus_chunk_seq),
            0,
        )
        win_start = max(0, focus_idx - _VISIBLE_FIRST_ABOVE)
        win_end = min(len(chunks), focus_idx + _VISIBLE_FIRST_BELOW + 1)

        # Newly-mounted "above-window" widgets get hidden until phase 2b
        # finishes; the finally block makes sure every entry in this
        # list ends up displayed even on cancellation.
        hidden_widgets: list[Widget] = []

        try:
            # Phase 1: sync mount of the visible window. Mount in
            # document order so widgets in the container are in
            # ascending chunk_seq from the start.
            for i in range(win_start, win_end):
                if i in container.mounted_indices:
                    continue
                self._mount_chunk_into(container, chunks[i], i, chunks)
            # Scroll to focused chunk before yielding.
            self._scroll_preview_to_chunk(focus_chunk_seq)
            self._update_progress_bar(progress=len(container.mounted_indices))
            await asyncio.sleep(0)

            # Phase 2a: background fill BELOW the window, in order.
            # No scroll shift since these append below visible content.
            for i in range(win_end, len(chunks)):
                if i in container.mounted_indices:
                    continue
                self._mount_chunk_into(container, chunks[i], i, chunks)
                self._update_progress_bar(progress=len(container.mounted_indices))
                # Yield every 3 chunks for cancellation responsiveness.
                if (i - win_end + 1) % 3 == 0:
                    await asyncio.sleep(0)
            await asyncio.sleep(0)

            # Phase 2b: hidden-prepend ABOVE the window. Each newly-
            # mounted widget gets ``display = False`` immediately, so
            # it takes no layout space and the focused chunk doesn't
            # drift while the rest of the doc loads. Yields are larger
            # here because hidden widgets cost almost nothing to add.
            for i in range(win_start - 1, -1, -1):
                if i in container.mounted_indices:
                    continue
                before = set(container.children)
                self._mount_chunk_into(container, chunks[i], i, chunks)
                for w in container.children:
                    if w not in before:
                        w.display = False
                        hidden_widgets.append(w)
                self._update_progress_bar(progress=len(container.mounted_indices))
                if (win_start - i) % 15 == 0:
                    await asyncio.sleep(0)
            await asyncio.sleep(0)

            # Reveal every hidden widget in one pass. We don't scroll
            # here — see the finally block. Hiding the progress bar
            # also removes the ``is-loading`` class which un-hides the
            # scrollbar; that itself can shift layout, so we want any
            # final scroll re-anchor to happen AFTER all of these
            # layout-affecting changes have settled.
            if hidden_widgets:
                for w in hidden_widgets:
                    w.display = True
                hidden_widgets.clear()
        finally:
            # Always reveal any widgets we hid; a cancelled task that
            # left them hidden would leak a half-displayed container
            # into the cache.
            for w in hidden_widgets:
                with contextlib.suppress(Exception):
                    w.display = True
            if container.is_complete:
                # Promote the container into the LRU cache.
                evicted = self._preview_cache.put(container)
                for old in evicted:
                    with contextlib.suppress(Exception):
                        old.remove()
                self._hide_progress_bar()
                # Re-anchor scroll to the user's selected chunk AFTER
                # the reveal + bar-hide layout changes have queued.
                # We don't trust ``virtual_region.y`` to be fresh yet,
                # so chain two ``call_after_refresh``s — that buys a
                # full extra render cycle for layout to finish before
                # the actual ``scroll_to_widget`` call fires.
                target_seq = focus_chunk_seq

                def _schedule_anchor() -> None:
                    self.call_after_refresh(self._scroll_preview_to_chunk, target_seq)

                self.call_after_refresh(_schedule_anchor)
            else:
                # Partial — leave the bar visible (a revisit will
                # resume); but if no task will resume (cancellation
                # because user moved on), the next _show_progress_bar
                # for the new file will overwrite our state.
                pass
            self._refresh_status()

    def _mount_chunk_into(
        self,
        container: PreviewContainer,
        chunk: FileChunk,
        index: int,
        all_chunks: list[FileChunk],
    ) -> None:
        """Mount one chunk widget into ``container`` at the position
        implied by its index. Updates the container's
        ``mounted_indices`` / ``chunk_widgets`` / ``match_targets``
        bookkeeping."""
        # Find the smallest already-mounted index greater than this one
        # — we mount BEFORE that widget so chunks stay in document
        # order regardless of which phase mounts them.
        before_widget: Static | None = None
        next_mounted = min(
            (j for j in container.mounted_indices if j > index),
            default=-1,
        )
        if next_mounted >= 0:
            before_seq = all_chunks[next_mounted].chunk_seq
            before_widget = container.chunk_widgets.get(before_seq)

        is_markdown = chunk.kind == "md"
        # Save current widgets-by-chunk_seq so the mount helpers fill
        # the per-container dicts (they update self._chunk_widgets /
        # self._match_targets, which are aliased to the active
        # container's dicts).
        if is_markdown:
            self._mount_markdown_chunk(container, chunk, before=before_widget)
        else:
            self._mount_plain_chunk(container, chunk, before=before_widget)
        container.mounted_indices.add(index)

    def _refresh_match_scrollbar(self, chunks: list[FileChunk]) -> None:
        """Build a per-chunk match map and forward it to the preview's
        custom scrollbar so chunk-match positions are visible on the bar."""
        from acorn.render import _term_stems, _terms_from_query, text_has_match

        try:
            pane = self.query_one("#preview_pane", MatchAwareScroll)
        except Exception:
            return
        term_stems = _term_stems(_terms_from_query(self._current_query))
        match_map = [
            bool(term_stems and any(text_has_match(b.text, term_stems) for b in c.blocks))
            for c in chunks
        ]
        pane.set_match_map(match_map)

    def _mount_chunks_for_file(self, parent_id: str, chunks: list[FileChunk]) -> None:
        """Legacy synchronous mount path retained for tests that exercise
        the rendering surface directly. The interactive flow now uses
        :meth:`_mount_chunks_async` (visible-first + background fill);
        this entry point clears the pane and mounts everything at once
        into a fresh :class:`PreviewContainer`.
        """
        pane = self.query_one("#preview_pane", VerticalScroll)
        for w in list(pane.children):
            w.remove()
        container = PreviewContainer(
            parent_doc_id=parent_id,
            query_signature=self._current_query_signature(),
            total_chunks=len(chunks),
        )
        pane.mount(container)
        self._activate_preview_container(container)
        if not chunks:
            return
        first_chunk = chunks[0]
        title = Static(Path(first_chunk.path).name, classes="preview-title")
        container.mount(title)
        is_markdown = first_chunk.kind == "md"
        for i, c in enumerate(chunks):
            if is_markdown:
                self._mount_markdown_chunk(container, c)
            else:
                self._mount_plain_chunk(container, c)
            container.mounted_indices.add(i)

    def _mount_plain_chunk(
        self,
        parent: Container | VerticalScroll,
        c: FileChunk,
        *,
        before: Static | None = None,
    ) -> None:
        """Per-line layout for non-markdown chunks. Each body line becomes
        its own Static so ``scroll_to_widget`` can target the first matched
        line, and the match-row gets a subtle accent overlay.

        We deliberately don't mount the locator header (``p. 351 · ...``)
        — that information lives on the sidebar result row; repeating it
        per-chunk in the preview is just visual clutter. The first body
        widget of each chunk gets a ``chunk-first`` class so a small top
        gap still marks the chunk boundary.

        ``before`` (if supplied) makes every widget mount immediately
        before that anchor — used by background-fill prepending so
        chunks land in document order even when mounted out of sequence.
        """
        _, pieces = render_chunk_pieces(c, query=self._current_query)
        first_widget: Static | None = None
        first_match: Static | None = None
        for line_text, has_match in pieces:
            line_w = Static(line_text, classes="chunk-line")
            line_w.acorn_text = line_text  # type: ignore[attr-defined]
            if has_match:
                line_w.add_class("chunk-line-match")
            parent.mount(line_w, before=before)
            if first_widget is None:
                line_w.add_class("chunk-first")
                first_widget = line_w
            if has_match and first_match is None:
                first_match = line_w
        if first_widget is None:
            return
        self._chunk_widgets[c.chunk_seq] = first_widget
        self._match_targets[c.chunk_seq] = first_match or first_widget

    def _mount_markdown_chunk(
        self,
        parent: Container | VerticalScroll,
        c: FileChunk,
        *,
        before: Static | None = None,
    ) -> None:
        """Markdown chunks: pretty rendering for context, per-line layout
        for matches.

        Matched chunks switch to per-line widgets (same path PDFs use)
        so query terms get the yellow-on-bold highlight. Non-matched
        chunks render through ``rich.markdown.Markdown`` so headings,
        code blocks, tables, lists, bold/italic still look right while
        the user scrolls for context.
        """
        from rich.markdown import Markdown

        from acorn.render import _term_stems, _terms_from_query, render, text_has_match

        terms = _terms_from_query(self._current_query)
        term_stems = _term_stems(terms)
        has_match = bool(term_stems and any(text_has_match(b.text, term_stems) for b in c.blocks))

        if has_match:
            self._mount_md_per_line(parent, c, before=before)
            return

        md_source = render(c.blocks, query="")
        body_w = Static(
            Markdown(md_source, code_theme="monokai"),
            classes="chunk-section chunk-md-body chunk-first",
        )
        parent.mount(body_w, before=before)
        self._chunk_widgets[c.chunk_seq] = body_w
        self._match_targets[c.chunk_seq] = body_w

    def _mount_md_per_line(
        self,
        parent: Container | VerticalScroll,
        c: FileChunk,
        *,
        before: Static | None = None,
    ) -> None:
        """Per-line markdown layout used when a chunk contains matches."""
        _, pieces = render_chunk_pieces(c, query=self._current_query)
        first_line: Static | None = None
        first_match: Static | None = None
        for line_text, has_match in pieces:
            line_w = Static(line_text, classes="chunk-line")
            line_w.acorn_text = line_text  # type: ignore[attr-defined]
            if has_match:
                line_w.add_class("chunk-line-match")
            parent.mount(line_w, before=before)
            if first_line is None:
                line_w.add_class("chunk-first")
                first_line = line_w
            if has_match and first_match is None:
                first_match = line_w
        target = first_match or first_line
        if target is None:
            return
        self._chunk_widgets[c.chunk_seq] = first_line or target
        self._match_targets[c.chunk_seq] = target

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
        if ctx == "filters":
            return self.query_one("#filters_panel_tree", Tree)
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
                sources=list(self._active_sources),
                collapsed_panels=sorted(self._collapsed_panels),
                filter_kinds=list(self._filter_kinds),
                filter_date=self._filter_date,
            )
        )

    # ── Collections panel (UX-D) ─────────────────────────────────

    def _refresh_collections_panel(self) -> None:
        """Repopulate the lazygit-style collections panel from the loaded
        Config, marking active collections AND active sources within
        them."""
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
        active_collections = set(self._collections)
        active_sources = set(self._active_sources)
        tree.show_root = False
        tree.clear()
        active_source_count = 0
        total_source_count = 0
        for name in names:
            col = cfg.collections[name] if cfg else None
            marker = "●" if name in active_collections else "○"
            n_sources = len(col.sources) if col else 0
            total_source_count += n_sources
            label = f"{marker}  {name}  ({n_sources} source{'s' if n_sources != 1 else ''})"
            node = tree.root.add(label, data={"kind": "collection", "name": name}, expand=False)
            if col:
                for i, s in enumerate(col.sources):
                    source_id = str(Path(str(s.path)).expanduser().resolve())
                    src_active = source_id in active_sources
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
        title = f"Collections — {len(active_collections)}/{len(names)} active"
        if total_source_count and active_source_count:
            title += f", {active_source_count}/{total_source_count} sources"
        tree.border_title = title

    # ── Filters panel (UX-F) ──────────────────────────────────────

    def _refresh_filters_panel(self) -> None:
        """Repopulate the Filters panel.

        Two top-level branches: ``File type`` (multi-select) and
        ``Date`` (radio). Each value row carries enough data on its
        node to round-trip back to ``_on_filters_panel_selected``
        without re-parsing labels.
        """
        try:
            tree = self.query_one("#filters_panel_tree", Tree)
        except Exception:
            return
        # Preserve which branches the user had open so a refresh after a
        # toggle doesn't snap the panel back to all-collapsed.
        was_expanded: set[str] = set()
        for branch in tree.root.children:
            data = branch.data if isinstance(branch.data, dict) else {}
            cat = data.get("category")
            if isinstance(cat, str) and branch.is_expanded:
                was_expanded.add(cat)
        tree.show_root = False
        tree.clear()

        active_kinds = set(self._filter_kinds)
        kind_summary = f"{len(active_kinds)} of {len(_FILTER_KINDS)}" if active_kinds else "any"
        kind_node = tree.root.add(
            f"File type        ({kind_summary})",
            data={"kind": "filter_category", "category": "kinds"},
            expand="kinds" in was_expanded,
        )
        for k in _FILTER_KINDS:
            marker = "●" if k in active_kinds else "○"
            kind_node.add_leaf(
                f"{marker}  {k}",
                data={"kind": "filter_value", "category": "kinds", "value": k},
            )

        date_summary = self._filter_date or "any"
        date_node = tree.root.add(
            f"Modified         ({date_summary})",
            data={"kind": "filter_category", "category": "date"},
            expand="date" in was_expanded,
        )
        for d in _FILTER_DATES:
            marker = "●" if d == self._filter_date else "○"
            date_node.add_leaf(
                f"{marker}  {d}",
                data={"kind": "filter_value", "category": "date", "value": d},
            )

        # Header tracks whether anything is filtering; the dim default
        # keeps the panel quiet when no filters are active.
        active_bits: list[str] = []
        if active_kinds:
            active_bits.append(f"{len(active_kinds)} kind{'s' if len(active_kinds) != 1 else ''}")
        if self._filter_date and self._filter_date != "any":
            active_bits.append(self._filter_date)
        title = "Filters" if not active_bits else f"Filters — {', '.join(active_bits)}"
        tree.border_title = title

    @on(Tree.NodeSelected, "#filters_panel_tree")
    def _on_filters_panel_selected(self, ev: Tree.NodeSelected[dict[str, object]]) -> None:
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
            if value in self._filter_kinds:
                self._filter_kinds.remove(value)
            else:
                self._filter_kinds.append(value)
        elif category == "date":
            self._filter_date = value
        else:
            return
        self._refresh_filters_panel()
        self._refresh_status()
        self._persist_state()
        if self._current_query:
            self._run_query(self._current_query)

    @on(Tree.NodeSelected, "#collections_panel_tree")
    def _on_collections_panel_selected(self, ev: Tree.NodeSelected[dict[str, object]]) -> None:
        """Enter on a collection node toggles the collection's scope.
        Enter on a source node toggles that single source's scope.
        Per the user's explicit request: Enter, not Space."""
        data = ev.node.data or {}
        kind = data.get("kind")
        if kind == "collection":
            name = str(data.get("name") or "")
            if not name:
                return
            if name in self._collections:
                self._collections.remove(name)
            else:
                self._collections.append(name)
        elif kind == "source":
            source_id = str(data.get("source_id") or "")
            if not source_id:
                return
            if source_id in self._active_sources:
                self._active_sources.remove(source_id)
            else:
                self._active_sources.append(source_id)
        else:
            return
        self._ranking_profile = self._resolve_profile()
        self._refresh_collections_panel()
        self._refresh_status()
        self._persist_state()
        if self._current_query:
            self._run_query(self._current_query)

    def action_dismiss_overlay(self) -> None:
        """Close any open overlay (help, picker, palette, explain, multi).
        No-op if none."""
        for selector in (
            "#help_overlay",
            "#collection_picker",
            "#cmd_palette",
            "#explain_overlay",
            "#multi_panel",
        ):
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

    def action_show_explain_overlay(self) -> None:
        """Toggle a JSON trace overlay for the most-recent search.

        Mirrors the help-overlay style: a Vertical with an embedded
        Markdown widget rendering the SearchTrace as a fenced ``json``
        block. The trace covers the entire search call, not just the
        focused hit — focused-hit details are visible in the trace's
        per-hit ``contributions`` list (UX-pass-4 §2).
        """
        existing = self.query("#explain_overlay")
        if existing:
            for w in existing:
                w.remove()
            return
        if self._latest_trace is None:
            self.notify(
                "no search yet — type a query first",
                severity="warning",
                title="Explain",
            )
            return
        import json

        from textual.widgets import Markdown as _Md

        body = json.dumps(self._latest_trace.to_json(), indent=2)
        md = (
            f"# Explain — `{self._latest_trace.query}`\n\n"
            f"Regime: **{self._latest_trace.regime}**\n\n"
            f"```json\n{body}\n```\n"
        )
        overlay = Vertical(_Md(md), id="explain_overlay")
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

    def action_open_multi_input(self) -> None:
        """Open the :multi DSL panel for typed sub-queries + intent line.

        Submit (Ctrl+J) parses via :func:`acorn.fusion.parse_multi_input`,
        sets ``_current_intent`` + ``_current_subqueries`` on the app,
        and runs the search. Empty submit closes the panel.
        """
        from textual.widgets import TextArea

        existing = self.query("#multi_panel")
        if existing:
            for w in existing:
                w.remove()
            return
        editor = TextArea(
            text="intent: \nlex: \nphrase: \nsyn: \n",
            id="multi_panel_input",
            language=None,
        )
        wrapper = Vertical(editor, id="multi_panel")
        self.mount(wrapper)
        editor.focus()

    def action_submit_multi_input(self) -> None:
        """Parse and apply the :multi panel's contents, then run search."""
        from textual.widgets import TextArea

        from acorn.fusion import parse_multi_input

        existing = self.query("#multi_panel_input")
        if not existing:
            return
        editor = self.query_one("#multi_panel_input", TextArea)
        text = editor.text
        for w in self.query("#multi_panel"):
            w.remove()
        result = parse_multi_input(text, synonyms=self._synonyms)
        self._current_intent = result.intent
        # Use lex line(s) as the search query (auto_subqueries inside
        # fusion_search will re-derive phrase + syn from this). Keeping
        # intent-only as the UX-pass-4 §3 hook; explicit sub-query
        # override is a future extension.
        lexical_parts = [s.query for s in result.subqueries if s.source == "lex"]
        if lexical_parts:
            self._run_query(" ".join(lexical_parts))

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

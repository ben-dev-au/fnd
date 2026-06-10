"""FND TUI — phase 5 shell.

Layout (per §5 wireframe):

  ┌─ Status bar (collection · result count) ─┐
  ├─ Query input ────────────────────────────┤
  ├─ Results tree (left)  │  Preview pane ──┤
  └──────────────────────────────────────────┘
   /  search   Tab  focus   ⏎  open   z  reading-view   o  default-app   q  quit

Phase 5 ships the structural layout + opener wired to Enter; phase 6 adds
the full action map (filter chips, command palette, customisable keymap),
phase 7 adds reranker live-tuning.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from rich.text import Text

    from fnd.synonyms import SynonymTable

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.scrollbar import ScrollBar
from textual.widget import Widget
from textual.widgets import (
    Input,
    Static,
    Tree,
)
from textual.widgets.tree import TreeNode

from fnd import opener
from fnd.config import Config, default_index_dir
from fnd.explain import SearchTrace
from fnd.matching import MatchSpec
from fnd.query import FileChunk, FileGroup, Hit, Searcher
from fnd.render import (
    render_chunk_pieces,
)
from fnd.rerank import RankingProfile
from fnd.tui.actions import REGISTRY, Keymap, load_keymap
from fnd.tui.indexer_service import IndexerService
from fnd.tui.line_buffer import (
    FileView,
    LineBufferPreview,
    RenderedDocument,
    build_rendered_document,
)
from fnd.tui.preview.flat_view import FlatBufferView
from fnd.tui.preview_dispatcher import choose_preview_mode, uses_markdown_renderer
from fnd.tui.preview_scroll import (
    FlatScrollStrategy,
    PreviewScrollController,
    ScrollAnchor,
    ScrollStrategy,
    StructuralScrollStrategy,
)
from fnd.tui.preview_scrollbar import MatchAwareScroll, ThinScrollBarRender
from fnd.tui.progress import FNDProgressBar, ProgressFacility, ProgressSession
from fnd.tui.results_labels import (
    _elide_middle_keep_suffix,
)
from fnd.tui.results_labels import _format_hit_label as _format_hit_label
from fnd.tui.results_labels import _score_bar as _score_bar
from fnd.tui.results_labels import _trim_redundant_heading as _trim_redundant_heading

# Single-name self-alias imports below are deliberate re-exports: tests and
# sibling modules historically import these names from fnd.tui.app.
from fnd.tui.results_view import ResultsView
from fnd.tui.scope_panel import ScopeController
from fnd.tui.search_controller import SearchController
from fnd.tui.widgets.markdown import (
    FNDMarkdown,
    _legacy_blocks_to_md,
)
from fnd.tui.widgets.markdown import FNDMarkdownFence as FNDMarkdownFence
from fnd.tui.widgets.markdown import FNDMarkdownH1 as FNDMarkdownH1
from fnd.tui.widgets.markdown import FNDMarkdownH2 as FNDMarkdownH2
from fnd.tui.widgets.markdown import FNDMarkdownH3 as FNDMarkdownH3
from fnd.tui.widgets.markdown import FNDMarkdownH4 as FNDMarkdownH4
from fnd.tui.widgets.markdown import FNDMarkdownH5 as FNDMarkdownH5
from fnd.tui.widgets.markdown import FNDMarkdownH6 as FNDMarkdownH6
from fnd.tui.widgets.markdown import FNDMarkdownParagraph as FNDMarkdownParagraph
from fnd.tui.widgets.markdown import FNDMarkdownTableDT as FNDMarkdownTableDT
from fnd.tui.widgets.markdown import FNDMarkdownTH as FNDMarkdownTH
from fnd.tui.widgets.markdown import _build_match_spans as _build_match_spans
from fnd.tui.widgets.markdown import _compute_table_col_widths as _compute_table_col_widths
from fnd.tui.widgets.markdown import _HeadingMarkerMixin as _HeadingMarkerMixin
from fnd.tui.widgets.markdown import _record_first_match as _record_first_match
from fnd.tui.widgets.preview_container import (
    _PREVIEW_CACHE_MAX_FILES,
    PreviewCache,
    PreviewContainer,
    _HitWithQuery,
)
from fnd.tui.widgets.preview_container import _PREVIEW_CACHE_MIN_CHUNKS as _PREVIEW_CACHE_MIN_CHUNKS
from fnd.tui.widgets.results_tree import ResultsTree

# App-wide thin scrollbars: every stock Textual ScrollBar (results/sidebar
# trees, code fences, settings lists) renders the thumb as a hairline glyph
# hugging the frame instead of a reverse-video full-cell block. The preview's
# MatchAwareScrollBar applies the same thinning via its own renderer subclass.
ScrollBar.renderer = ThinScrollBarRender

# Visible-first mount window — chunks are decoded already, mounting
# focused ± these counts synchronously gives the user instant viewport
# feedback before the background fill starts.
_VISIBLE_FIRST_ABOVE = 7
_VISIBLE_FIRST_BELOW = 7
# Background-fill bound, applied beyond the ±_VISIBLE_FIRST_* window
# during the initial cold mount. At < _VISIBLE_FIRST_* the phase 2a/2b
# loops are no-ops; the scroll-driven lazy mount picks up from the
# visible-window boundary instead. Raise to e.g. 10 for a small static
# buffer before lazy-mount engages (Stage 0a in PREVIEW_DOM_PLAN.md);
# the trade-off is a small cold-mount cost per cached file.
_BACKGROUND_FILL_RADIUS = 3
# Option C: when the active file is within this many chunks, background-fill it
# completely so internal match-jumps land on an already-mounted chunk (instant).
# Larger files stay windowed (radius above) to protect DOM size / input lag.
_FULLMOUNT_CHUNK_BUDGET = 250
# Prefetch mounts only the focused chunk per cached file. User-side
# resume expands on click via Phase 1b/2. Keeps prefetch DOM
# contribution at ~1 widget per cached file.
_PREFETCH_MOUNT_RADIUS = 0
# Scroll-driven lazy mount. When the user scrolls within this many
# cells of the boundary of the mounted region, the next batch is
# mounted on demand. Lets long files behave like a continuous document
# without forcing the initial mount to cover everything.
_LAZY_MOUNT_TRIGGER_MARGIN = 30
_LAZY_MOUNT_BATCH = 3
# Scroll-to-match leaves this fraction of the viewport above the match so
# the user sees context before it, rather than pinning it to the top line.
_MATCH_CONTEXT_FRACTION = 0.25


# Per-chunk renderer choice lives in ``preview_dispatcher`` so the
# file-level ``choose_preview_mode`` decision and the per-chunk mount
# loop can't drift apart on which kinds are markdown-rendered.
_uses_markdown_renderer = uses_markdown_renderer


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


def _action_priority(action_id: str) -> bool:
    for a in REGISTRY:
        if a.id == action_id:
            return a.priority
    return False


def render_hint_bar(
    anchors: tuple[tuple[str, str], ...],
    contextual: tuple[tuple[str, str], ...] = (),
) -> Any:
    """Build the bottom hint bar as a Rich ``Text``.

    Two clusters separated by extra whitespace: ``anchors`` on the left
    (always present, builds muscle memory), ``contextual`` on the right
    (changes by focus / screen). Both use the same key-glyph rendering
    so the visual is identical across the main app and the Settings
    menu — this is the renderer both call into.
    """
    from rich.text import Text

    def _cluster(pairs: tuple[tuple[str, str], ...]) -> Text:
        sep = Text("  │  ", style="dim")
        out = Text("")
        for i, (key, label) in enumerate(pairs):
            if i:
                out.append_text(sep)
            out.append_text(Text.from_markup(f"[reverse] {key} [/] {label}"))
        return out

    joined = _cluster(anchors)
    if contextual:
        joined.append_text(Text("      ", style=""))
        joined.append_text(_cluster(contextual))
    return joined


class FNDApp(App[None]):
    """Phase 5 shell."""

    CSS = """
    Screen { background: $surface; }
    #query_bar { height: 1; padding: 0 1; border: none; }
    #query_notice { height: auto; padding: 0 1; color: $warning; display: none; }
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
    /* Wrap fenced code instead of scrolling it horizontally. Textual's
       stock MarkdownFence sets ``overflow: scroll hidden`` and lets its
       inner Label size to the longest line, so long lines spill into a
       horizontal scrollbar. Hiding overflow-x and pinning the Label to
       the fence width makes the content reflow to the pane — no
       horizontal bar to chase. The bottom padding is dropped (the stock
       ``padding: 1 2`` left a dark backdrop row that read as a thick
       scrollbar) and the vertical bar stays the app-wide hairline. */
    MarkdownFence {
        overflow-x: hidden;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 0;
        padding: 0 0 0 1;
    }
    /* Inset lives on the fence, not the Label: a Label with side padding +
       width: 1fr wraps to its border-box width, clipping ~2 cells of code.
       Padding the fence keeps the 1-col left inset (readability) while the
       Label wraps cleanly to the inset content width. */
    MarkdownFence > Label {
        padding: 0;
        width: 1fr;
    }
    /* Rendered mermaid diagrams can't wrap (box-drawing breaks), so a wide
       one keeps its width and gets a thin horizontal scrollbar instead of
       being clipped to blank. The Label sizes to content, not the fence. */
    FNDMarkdownFence.mermaid-diagram {
        overflow-x: auto;
        scrollbar-size-horizontal: 1;
    }
    FNDMarkdownFence.mermaid-diagram > Label {
        width: auto;
    }
    /* Pane borders dim by default, brighten when the pane (or any
       descendant) is focused — lazygit's active-section convention.
       Results expands to fill remaining space; Collections + Filters
       size to their content so each panel stays exactly as tall as
       its rows demand. */
    #results_column { width: 1fr; height: 1fr; }
    /* Cancel Textual's focused-Tree tint — border colour is the focus signal. */
    #results_column Tree {
        background-tint: transparent;
    }
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
       the collapsed height when the inner tree has more rows than fit.
       The results pane intentionally keeps a 1-row content area
       (height: 3 = top border + 1 row + bottom border) so the
       currently-selected hit — which drives the preview pane — stays
       visible. The secondary panels (collections, filters) drop to
       just the two border rows so their cursor highlight doesn't bleed
       through and read as "still selected" when the panel is closed. */
    #results_pane.collapsed {
        height: 3;
        overflow: hidden;
        scrollbar-size-vertical: 0;
        scrollbar-size-horizontal: 0;
    }
    #collections_panel_tree.collapsed,
    #filters_panel_tree.collapsed {
        height: 2;
        overflow: hidden;
        scrollbar-size-vertical: 0;
        scrollbar-size-horizontal: 0;
    }
    /* Right-side preview column: just the scrollable pane now. The
       progress strip moved to app level (above the footer hints) so
       toggling it doesn't reflow the preview. */
    #preview_column { width: 2fr; height: 1fr; }
    #preview_pane {
        width: 100%; height: 1fr;
        border: round $primary 50%;
        padding: 0 0 0 1;
    }
    /* Class-toggled focus border — :focus-within would re-style every descendant. */
    #preview_pane.-focused { border: round $accent; }
    /* Reading view: drop the border + padding so a full-width terminal
       selection copies only the text, not the frame. Listed after
       ``-focused`` so it wins at equal specificity when both apply.
       Zero the pane's own scrollbar too: for flat-buffer previews the
       inner LineBufferPreview already shows the match-marker bar, so the
       pane's bar is a bare duplicate. */
    #preview_pane.-reading { border: none; padding: 0; scrollbar-size-vertical: 0; }
    /* While a partial mount is in flight we hide the scrollbar (its
       virtual size keeps growing as chunks land, so the thumb would
       jitter). Programmatic ``scroll_to_widget`` calls during phase 2b
       still need to work — using ``overflow-y: hidden`` would prevent
       that, so we only suppress the bar's chrome, not scrolling. */
    #preview_pane.is-loading { scrollbar-size-vertical: 0; }
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
        Binding("escape", "escape_back", "Back", show=False),
        Binding("ctrl+shift+d", "diag_dump_preview", "Dump preview widget tree", show=False),
        *(
            Binding(
                key,
                action_id,
                _short_label(action_id),
                show=_action_show(action_id),
                priority=_action_priority(action_id),
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
        self._initial_query = initial_query
        # Search state + orchestration (searcher, query, match spec,
        # result groups, trace); see fnd/tui/search_controller.py.
        self._search = SearchController(self)
        # Results-tree rendering; see fnd/tui/results_view.py.
        self._results = ResultsView(self)
        self._fnd_keymap = keymap or load_keymap()
        # Ranking profile applied at search time. Built from the active
        # collection's ``ranking_profile`` field; default profile (all-zero)
        # is the BM25 identity, so the no-config case is unchanged.
        self._config = config
        # Scope + sidebar panel state (collections / sources / filters and
        # the panel layout); reads ``_config`` for source-id resolution.
        self._scope = ScopeController(self, collection=collection)
        # Reading mode: hide the sidebar so the preview fills the width for
        # clean text selection / distraction-free reading. Session-only;
        # owns mouse capture (off while reading so the terminal handles
        # drag-select, right-click Copy, ⌘C, macOS Speak-selection).
        self._reading_mode: bool = False
        self._ranking_profile = self._resolve_profile()
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
        # The previously-visible container, kept on screen during a cold/swap
        # mount so the pane never blanks: the incoming container builds
        # invisibly (opacity:0) and only when its scroll lands do we hide this
        # one and reveal the new one in a single tick. Cleared by that swap.
        self._outgoing_preview: PreviewContainer | None = None
        # Flat (line-buffer) preview path: the shared widget, its value
        # cache, and the install/activate lifecycle; see
        # fnd/tui/preview/flat_view.py.
        self._flat = FlatBufferView(self)
        # Background indexer lifecycle (task / cancel / events + the
        # update-all chain bookkeeping); see fnd/tui/indexer_service.py.
        # The modal reads this through the app's _indexer_* accessors.
        self._indexer = IndexerService(self)
        # Structured-PDF extras install/uninstall — sibling to the
        # indexer task so the modal can be dismissed (Background) and
        # reopened against the live task. See
        # fnd/tui/extras_install_progress.py.
        self._extras_task: asyncio.Task[None] | None = None
        self._extras_cancel: asyncio.Event | None = None
        self._extras_events: asyncio.Queue[Any] | None = None
        self._extras_last_event: Any = None
        self._extras_proc: Any = None
        self._extras_action_label: str = ""
        # Convenience aliases that point into the active container —
        # legacy code paths (_scroll_preview_to_chunk, etc.) read from
        # these instead of poking at the container directly.
        # Widgets here may be either per-line ``Static``s (PDF / TXT
        # plain renderer) or whole-chunk ``FNDMarkdown`` widgets (md
        # / docx / pptx structural renderer). The dict is widened to
        # ``Widget`` so both can be stored without complaint.
        self._chunk_widgets: dict[int, Widget] = {}
        self._match_targets: dict[int, Widget] = {}
        # Owns the structural preview scroll-to-match logic, reading the
        # chunk/match maps and pane back off this app via the host accessors.
        self._preview_scroll_structural = StructuralScrollStrategy(host=self)
        self._preview_scroll_flat = FlatScrollStrategy(host=self)
        # Single source of truth for where the preview should sit: navigation
        # arms an anchor; mount/finalize events reconcile against it (idempotent
        # → the formerly racing scroll sites collapse to one target).
        self._preview_scroll = PreviewScrollController(select_strategy=self._select_scroll_strategy)
        # Set around the controller's own structural scroll so the resulting
        # scroll-watcher trip isn't mistaken for a user scroll and doesn't
        # self-release the anchor.
        self._preview_scroll_reconciling: bool = False
        # The parent_id whose chunks are currently mounted in the preview
        # pane (so we don't re-mount when cursor moves within the same file).
        self._preview_parent_id: str | None = None
        # (loaded, total) while a chunk-decode + mount worker is running.
        self._preview_load_progress: tuple[int, int | None] | None = None
        # Strong ref so the event loop doesn't GC the in-flight mount task.
        self._preview_mount_task: object | None = None
        # In-flight lazy-mount task (driven by scroll). One at a time;
        # cleared on file switch alongside ``_preview_mount_task``.
        self._lazy_mount_task: object | None = None
        # Monotonic-time gate. Programmatic scrolls (navigation anchor,
        # finalize reveal) push this forward so the watcher doesn't
        # interpret their own scroll changes as user intent and fire a
        # competing mount that yanks the focused chunk off-screen.
        # Debounce timer so rapid scroll bursts collapse to a single
        # check at the tail end — protects programmatic intermediate
        # scrolls AND smooths user wheel/key scroll bursts.
        self._lazy_mount_check_timer: object | None = None
        # Prebuilt flat-buffer bundles keyed by (parent_id, query_sig).
        # Cleared on query change — highlight spans are baked in at build time.
        self._prebuilt_cache: dict[tuple[str, str], RenderedDocument] = {}
        # Debounced preview load — latest target + Timer.
        from typing import Any as _Any

        self._preview_load_timer: _Any | None = None
        self._preview_load_target: tuple[str, int] | None = None
        # The (parent_id, focus_chunk_seq) of the render currently in
        # flight, so redundant identical dispatches landing in the same
        # tick coalesce. Cleared when that render finishes settling.
        self._inflight_preview_target: tuple[str, int] | None = None
        self._progress = ProgressFacility(self)
        # Single-consumer drainer serializes prefetch widget-mounts.
        import asyncio as _asyncio

        self._prefetch_sink_queue: _asyncio.Queue[_Any] | None = None
        self._prefetch_sink_drainer: _Any | None = None

    def open_progress(self, phase: str = "", *, total: int = 1) -> ProgressSession:
        """Open a new ProgressSession. Use as a context manager."""
        return self._progress.open(phase, total=total)

    # ── Layout ────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Search…", id="query_bar", value=self._initial_query)
        # Calm, practical one-liner for malformed queries — collapsed until set.
        yield Static("", id="query_notice")
        with Horizontal():
            # Left column: results on top, then Collections, then Filters.
            with Vertical(id="results_column"):
                yield ResultsTree("Results", id="results_pane")
                # Single-widget panels — each carries its summary in
                # ``border_title``, matching the results-pane styling.
                # Collections tree uses the plain Tree (expanded
                # collection rows stay selectable — Enter toggles the
                # whole collection in/out of scope). Filters tree uses
                # the skip-expanded-parent subclass so File-type /
                # Modified headers behave the same as file rows.
                yield Tree("Collections", id="collections_panel_tree")
                yield ResultsTree("Filters", id="filters_panel_tree")
            # Right column: preview pane only. The progress strip lives
            # at app level (below) so it can be shared by every long
            # operation (preview load, indexing, cache rebuild) without
            # a layout shift in the preview pane each time it toggles.
            # Preview pane: MatchAwareScroll = VerticalScroll with a
            # custom scrollbar that overlays chunk-match markers on
            # the track. Per-file Containers hold the actual chunk
            # widgets so cache hits are O(1) display flips.
            with Vertical(id="preview_column"), MatchAwareScroll(id="preview_pane"):
                yield Static("Type a query and press Enter.", id="placeholder")
        # App-level progress strip — one row, full width, hidden via
        # ``visibility: hidden`` so the row occupancy is stable across
        # show / hide (no preview reflow). Driven by ``ProgressFacility``.
        yield FNDProgressBar()
        yield Static("", id="footer_hints")

    def _apply_mouse_capture(self, on: bool) -> None:
        """Toggle terminal mouse reporting at runtime. Called only on Reading
        View entry/exit: OFF hands selection back to the terminal so
        drag-select, right-click Copy, ⌘C and macOS Speak-selection work like
        any normal terminal app; ON restores the default clickable interface
        (click-to-focus, hover wheel-scroll, scrollbar drag). Guarded for
        headless/test drivers that lack the private hooks."""
        driver = getattr(self, "_driver", None)
        hook = getattr(
            driver,
            "_enable_mouse_support" if on else "_disable_mouse_support",
            None,
        )
        if callable(hook):
            hook()

    def action_toggle_reading_mode(self) -> None:
        """Hide the sidebar so the preview fills the full terminal width and
        release mouse capture: a normal terminal text selection then covers
        only the preview (clean copy for text-to-speech, ⌘C, right-click
        Copy), and it reads distraction-free. Also drops the preview
        border/padding so the frame isn't copied. Toggle to restore."""
        # Hiding the sidebar widens the preview, which re-wraps the content and
        # shifts the scroll position. Read the current reading position and
        # scroll back to it once the reflow lands — the same regardless of how
        # the position was reached (match nav or user scroll), so a reader who
        # scrolled away keeps their place rather than snapping back to the match.
        location = self._preview_scroll.locate()
        self._reading_mode = not self._reading_mode
        self.query_one("#results_column", Vertical).display = not self._reading_mode
        preview = self.query_one("#preview_pane", MatchAwareScroll)
        preview.set_class(self._reading_mode, "-reading")
        # Hand the terminal back its mouse so native selection / TTS work
        # inside reading view; restore capture (and thus hover-scroll /
        # click-to-focus) on exit.
        self._apply_mouse_capture(not self._reading_mode)
        if self._reading_mode:
            preview.focus()
        else:
            self.query_one("#results_pane", ResultsTree).focus()
        if location is not None:
            self.call_after_refresh(self._preview_scroll.scroll_to_location, location)
        self._refresh_footer_hints()

    def on_mount(self) -> None:
        # Tokyo-night theme: muted blue/teal pastel palette per user request.
        self.theme = "tokyo-night"
        import asyncio as _asyncio

        self._prefetch_sink_queue = _asyncio.Queue()
        self._prefetch_sink_drainer = _asyncio.create_task(self._drain_prefetch_sinks())

        # One-time: promote PDF-texture cache entries produced by the current
        # engine but under the pre-`tex-vN` key format to the coarse
        # signature, so current-engine work is recognised as current (not
        # "outdated"). Genuinely-older entries are left as-is and surface in
        # Settings for opt-in re-texturising. Sentinel-guarded → runs once.
        import contextlib as _contextlib

        with _contextlib.suppress(Exception):
            from fnd.cache import PdfStructureCache
            from fnd.extract.pdf import _config_hash, texture_signature

            _cache = PdfStructureCache()
            _sentinel = _cache.root / ".keys-migrated"
            # Only when a cache actually exists — never create the dir for a
            # fresh user (a freshly-written entry is already tex-vN anyway).
            if _cache.root.exists() and not _sentinel.exists():
                _migrated, _failed = _cache.promote_current_engine_entries(
                    current_sig=texture_signature(),
                    current_cfg_marker=f"cfg-{_config_hash()}",
                )
                # Mark done only on a clean pass; a partial promotion retries
                # next launch rather than stranding entries as "outdated".
                if _failed == 0:
                    _sentinel.write_text(texture_signature())

        # Route fnd.apps notices through an in-app modal for AX issues, and
        # through Textual.notify for everything else. Without this hook, the
        # Preview handler's stderr fallback gets buried under the curses
        # display and the user has no idea the page-jump failed.
        from fnd import apps as _apps_mod

        _apps_mod.set_notice_sink(self._dispatch_apps_notice)

        try:
            self._searcher = Searcher(index_dir=self._index_dir)
        except (FileNotFoundError, RuntimeError):
            # No index yet — the app still opens so the user can manage
            # collections, then reindex outside or from the CLI.
            self._searcher = None
        tree = self.query_one("#results_pane", Tree)
        tree.show_root = False
        tree.guide_depth = 2
        # Results parents (file rows) are not cursor-selectable when expanded —
        # the cursor bounces past them onto the first hit. See
        # ``_skip_expanded_parent``.
        tree._skip_expanded_parents = True  # type: ignore[attr-defined]
        # Collections panel — populated from the loaded Config.
        ctree = self.query_one("#collections_panel_tree", Tree)
        ctree.show_root = False
        ctree.guide_depth = 2
        # Collections parents stay cursor-selectable so Enter still toggles
        # the whole collection (`_on_collections_panel_selected`).
        ctree._skip_expanded_parents = False  # type: ignore[attr-defined]
        # Enter must not auto-expand — that fires NodeExpanded and
        # persists state the user didn't ask for. Right still expands
        # via action_tree_smart_expand.
        ctree.auto_expand = False
        self._refresh_collections_panel()
        # Filters panel — kind/date selectors that compose into the query.
        ftree = self.query_one("#filters_panel_tree", Tree)
        ftree.show_root = False
        ftree.guide_depth = 2
        # Filters parents (File type / Modified) are no-ops on Enter — skip
        # past them when expanded.
        ftree._skip_expanded_parents = True  # type: ignore[attr-defined]
        self._refresh_filters_panel()
        # Restore persisted panel collapse-to-header.
        import contextlib

        for panel_id in self._collapsed_panels:
            with contextlib.suppress(Exception):
                self.query_one(f"#{panel_id}").add_class("collapsed")
        self._refresh_status()
        if self._initial_query:
            self._run_query(self._initial_query)
        if not self._initial_query or not self._groups:
            self.query_one("#query_bar", Input).focus()
        # Auto-resume any interrupted reindex from a previous fnd session.
        # Runs in background (no modal); user can click the footer
        # indicator or invoke `action_reindex_default` to view progress.
        # Wrapped in try/except so a corrupt state file doesn't keep the
        # TUI from launching.
        with contextlib.suppress(Exception):
            self._maybe_resume_indexer()
        # Pre-upgrade cache entries (PDFs textured on an older extractor
        # version) are surfaced passively in Settings → Indexing & PDF
        # Texture, not via a startup popup — re-texturising is a
        # preview-quality refresh the user opts into, never urgent.

    # ── Ranking profile (§7) ──────────────────────────────────────

    def _resolve_profile(self) -> RankingProfile:
        return self._search.resolve_profile()

    # ── Pane border titles ────────────────────────────────────────

    def _results_title(self) -> str:
        return self._results.title()

    def _preview_title(self, edge_width: int = 0) -> str:
        """Border title for the preview pane — ``Preview — <file>``.

        ``edge_width`` is the pane's outer border-box width; when given, the
        filename is middle-elided so its extension survives instead of being
        clipped off the right edge. A round border reserves 6 cells of the
        edge (2 corners + 2 pads + 2 filler dashes — measured), so the full
        title string must fit in ``edge_width - 6``.
        """
        if self._preview_parent_id is None:
            return "Preview"
        for g in self._groups:
            if g.parent_id == self._preview_parent_id:
                name = Path(g.path).name
                if edge_width > 0:
                    prefix = "Preview — "
                    name = _elide_middle_keep_suffix(name, edge_width - 6 - len(prefix))
                return f"Preview — {name}"
        return "Preview"

    def _refresh_status(self) -> None:
        try:
            self.query_one("#results_pane", Tree).border_title = self._results_title()
            pane = self.query_one("#preview_pane")
            pane.border_title = self._preview_title(pane.region.width)
        except Exception:
            pass
        self._refresh_footer_hints()

    def _dispatch_apps_notice(self, message: str) -> None:
        """Route a notice from fnd.apps through the right UI surface.

        Accessibility-permission denials get a modal so the user can act
        on them; everything else uses the inline notification toast.
        """
        if "Accessibility" in message or "accessibility" in message:
            from fnd.tui.ax_permission_screen import AccessibilityPermissionScreen

            self.push_screen(AccessibilityPermissionScreen())
            return
        self.notify(message, title="fnd", timeout=6)

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

    # Anchor cluster — keys that never move regardless of focus. Muscle
    # memory: search, settings menu, keybindings, quit. Sourced as a
    # tuple-of-tuples so the order is explicit.
    _FOOTER_ANCHORS: tuple[tuple[str, str], tuple[str, str], tuple[str, str], tuple[str, str]] = (
        ("/", "Search"),
        (":", "Menu"),
        ("?", "Keys"),
        ("q", "Quit"),
    )

    # Contextual cluster, keyed by focus context — what's *relevant* in
    # the pane that has focus right now. Empty list = no contextual hints
    # (anchor cluster alone).
    _FOOTER_CONTEXTUAL: ClassVar[dict[str, tuple[tuple[str, str], ...]]] = {
        "query": (
            ("Enter", "Run"),
            ("Esc", "Results"),
        ),
        "results": (
            ("o", "Open"),
            ("z", "Reading View"),
            ("Tab", "Search"),
        ),
        "preview": (
            ("j/k", "Scroll"),
            ("Esc", "Results"),
        ),
        "filters": (
            ("Enter", "Toggle"),
            ("←/→", "Collapse"),
            ("Esc", "Results"),
        ),
        "collections": (
            ("Enter", "Toggle"),
            ("←/→", "Collapse"),
            ("Esc", "Results"),
        ),
    }

    def _refresh_footer_hints(self) -> None:
        """Render the bottom hint bar from the anchor + per-focus tables.

        Delegates the actual rendering to :func:`render_hint_bar` so the
        Settings menu uses the same visual.
        """
        ctx = self._focus_context()

        # Overlay state (explain / multi DSL) preempts the per-pane table.
        # The settings menu lives on the screen_stack and renders its
        # own hint row, so we don't reach this branch when it's open.
        overlay_hint: tuple[tuple[str, str], ...] | None = None
        if self.query("#explain_overlay") or self.query("#multi_panel"):
            overlay_hint = (("Esc", "Close"),)

        contextual = (
            overlay_hint if overlay_hint is not None else self._FOOTER_CONTEXTUAL.get(ctx, ())
        )
        # Reading view focuses the preview, so the per-pane table would hide
        # the toggle key — surface the exit hint while it's active.
        if self._reading_mode and overlay_hint is None:
            contextual = (("z", "Reading View"), ("j/k", "Scroll"))

        import contextlib

        with contextlib.suppress(Exception):
            self.query_one("#footer_hints", Static).update(
                render_hint_bar(self._FOOTER_ANCHORS, contextual)
            )

    def on_descendant_focus(self) -> None:
        self._refresh_footer_hints()
        # Toggle the focus-border class without triggering a subtree style walk.
        try:
            pane = self.query_one("#preview_pane")
        except Exception:
            return
        in_preview = self._focus_context() == "preview"
        has_focus_class = "-focused" in pane.classes
        if in_preview == has_focus_class:
            return
        pane.set_class(in_preview, "-focused", update=False)
        self.app.stylesheet.apply(pane)

    def on_key(self, event: events.Key) -> None:
        """Repurpose Up/Down to navigate between sidebar panels when the
        focused panel is collapsed-to-header.

        Inside a collapsed tree the cursor row isn't visible anyway, so
        intra-tree Up/Down moves are noise. While collapsed they instead
        cycle focus to the previous/next ``Tree`` sibling in the left
        column (wrapping at the edges) so a user can sweep through
        panels with one hand on the arrow keys. Uncollapsed panels keep
        Textual's default Tree key handling untouched.
        """
        if event.key not in ("up", "down"):
            return
        tree = self._focused_tree()
        if tree is None or "collapsed" not in tree.classes:
            return
        try:
            column = self.query_one("#results_column", Vertical)
        except Exception:
            return
        panes = [w for w in column.children if isinstance(w, Tree)]
        if tree not in panes:
            return
        delta = 1 if event.key == "down" else -1
        target = panes[(panes.index(tree) + delta) % len(panes)]
        target.focus()
        event.stop()
        event.prevent_default()

    # ── Search flow ───────────────────────────────────────────────

    @on(Input.Submitted, "#query_bar")
    def _on_query_submit(self, ev: Input.Submitted) -> None:
        self._run_query(ev.value)

    def _run_query(self, query: str) -> None:
        self._search.run(query)

    # ── Search delegation (state lives on SearchController) ───────
    # Tests and sibling modules read AND write these names on the app;
    # the property pairs keep that surface stable while the controller
    # owns the state.

    @property
    def _searcher(self) -> Searcher | None:
        return self._search.searcher

    @_searcher.setter
    def _searcher(self, value: Searcher | None) -> None:
        self._search.searcher = value

    @property
    def _current_query(self) -> str:
        return self._search.current_query

    @_current_query.setter
    def _current_query(self, value: str) -> None:
        self._search.current_query = value

    @property
    def _current_match_spec(self) -> MatchSpec:
        return self._search.match_spec

    @_current_match_spec.setter
    def _current_match_spec(self, value: MatchSpec) -> None:
        self._search.match_spec = value

    @property
    def _highlights_enabled(self) -> bool:
        return self._search.highlights_enabled

    @_highlights_enabled.setter
    def _highlights_enabled(self, value: bool) -> None:
        self._search.highlights_enabled = value

    @property
    def _current_intent(self) -> str | None:
        return self._search.intent

    @_current_intent.setter
    def _current_intent(self, value: str | None) -> None:
        self._search.intent = value

    @property
    def _groups(self) -> list[FileGroup]:
        return self._search.groups

    @_groups.setter
    def _groups(self, value: list[FileGroup]) -> None:
        self._search.groups = value

    @property
    def _latest_trace(self) -> SearchTrace | None:
        return self._search.latest_trace

    @_latest_trace.setter
    def _latest_trace(self, value: SearchTrace | None) -> None:
        self._search.latest_trace = value

    @property
    def _synonyms(self) -> SynonymTable:
        return self._search.synonyms

    @_synonyms.setter
    def _synonyms(self, value: SynonymTable) -> None:
        self._search.synonyms = value

    @property
    def _ranking_profile(self) -> RankingProfile:
        return self._search.ranking_profile

    @_ranking_profile.setter
    def _ranking_profile(self, value: RankingProfile) -> None:
        self._search.ranking_profile = value

    def _refresh_results_tree(self) -> None:
        self._results.refresh()

    def on_resize(self, _event: events.Resize) -> None:
        """Re-fit elided filenames to the new pane widths. Deferred to after
        layout so the panes report their settled geometry."""
        self.call_after_refresh(self._refit_after_resize)

    def _refit_after_resize(self) -> None:
        self._results.refit_after_resize()

    # ── Preview ───────────────────────────────────────────────────

    @on(Tree.NodeHighlighted)
    def _on_tree_highlight(self, ev: Tree.NodeHighlighted[Any]) -> None:
        data: Any = ev.node.data
        if not isinstance(data, dict):
            return
        kind = data.get("kind")
        if kind == "section":
            hit: Hit = data["hit"]
            self._schedule_preview_load(hit.parent_id, hit.chunk_seq)
        elif kind == "file":
            g: FileGroup = data["group"]
            top = g.hits[0] if g.hits else None
            self._schedule_preview_load(g.parent_id, top.chunk_seq if top else 0)

    def _schedule_preview_load(self, parent_id: str, focus_chunk_seq: int) -> None:
        """Debounce a cursor-move → preview-load; coalesces rapid arrow sweeps."""
        # Preempt stale tail-mount on the previous file so the loop is
        # free during the debounce window.
        active_parent = (
            self._active_preview.parent_doc_id if self._active_preview is not None else None
        )
        if active_parent is not None and active_parent != parent_id:
            self._cancel_preview_mount_task()
            self._cancel_lazy_mount_task()
            # The cancelled mount will never reach settle to clear the in-flight
            # coalescing latch. If that latched target differs from where the
            # cursor is now heading, drop it — otherwise returning to it later
            # (an overshoot-and-correct sweep) hits the dedup guard as "already
            # in flight" and the remount is suppressed, stranding the preview
            # mid-mount until an unrelated nav resets the latch.
            if self._inflight_preview_target is not None and self._inflight_preview_target != (
                parent_id,
                focus_chunk_seq,
            ):
                self._inflight_preview_target = None
        self._preview_load_target = (parent_id, focus_chunk_seq)
        if self._config is not None:
            delay_ms = self._config.defaults.preview_load_debounce_ms
        else:
            from fnd.config import Defaults

            delay_ms = Defaults().preview_load_debounce_ms
        if delay_ms <= 0:
            self._fire_pending_preview_load()
            return
        if self._preview_load_timer is not None:
            with contextlib.suppress(Exception):
                self._preview_load_timer.stop()
        self._preview_load_timer = self.set_timer(
            delay_ms / 1000.0,
            self._fire_pending_preview_load,
            name="preview-load-debounce",
        )

    def _fire_pending_preview_load(self) -> None:
        self._preview_load_timer = None
        target = self._preview_load_target
        if target is None:
            return
        self._preview_load_target = None
        parent_id, focus_chunk_seq = target
        # Coalesce redundant identical loads. A query both parks the
        # cursor (which fires NodeHighlighted) AND dispatches explicitly
        # as a fallback for when the cursor index is unchanged, so the
        # same (parent, seq) load can land several times in one tick.
        # With the debounce pinned to 0 these don't merge; the 2nd+ then
        # warm-resume and cancel the 1st's still-building mount, orphaning
        # the focus chunk's build_done and losing the match scroll. If the
        # exact same render is already in flight, skip — it will land it.
        if self._inflight_preview_target == (parent_id, focus_chunk_seq):
            return
        self._inflight_preview_target = (parent_id, focus_chunk_seq)
        # Re-anchor prefetch around where the cursor actually settled
        # every time, not only on cache miss. Cursor-following: window
        # follows the user instead of waiting for them to outrun it.
        # Prefetch is an exclusive-group worker so the previous run is
        # cancelled cleanly.
        self._prefetch_top_results(anchor_parent_id=parent_id)
        self._render_full_doc(parent_id, focus_chunk_seq=focus_chunk_seq)

    def _cancel_pending_preview_load(self) -> None:
        if self._preview_load_timer is not None:
            with contextlib.suppress(Exception):
                self._preview_load_timer.stop()
            self._preview_load_timer = None
        self._preview_load_target = None

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

        from fnd.tui import _perf

        _perf.mark("click_to_display_start", parent_id=parent_id, focus_seq=focus_chunk_seq)

        # Any pending debounce timer is now moot — we're committing to
        # a load. Cancel so a late-firing timer can't race the current
        # dispatch and clobber it with a stale target.
        self._cancel_pending_preview_load()

        if self._searcher is None:
            return

        # Arm the scroll controller for this navigation. Every mount/finalize
        # event below reconciles against this one anchor instead of issuing its
        # own scroll, so call order can no longer change where the preview lands.
        # Glide smoothly only when the target match is ALREADY mounted (the
        # content between is on screen, so the scroll is over real rows). A
        # fresh file — or a same-file match outside the mounted window — is
        # revealed via an atomic swap (cut) instead: animating over an unmounted
        # gap would be lumpy, and prepending an out-of-window window above the
        # current match slides the view (reflow). Consistent rule: glide when
        # the content is there, cut when it must be built.
        active = self._active_preview
        target_mounted = (
            active is not None
            and active.parent_doc_id == parent_id
            and (active.is_complete or focus_chunk_seq in active.chunk_widgets)
        )
        self._preview_scroll.arm(ScrollAnchor(parent_id, focus_chunk_seq, animate=target_mounted))

        chunks = self._chunk_cache.get(parent_id)
        if chunks is not None:
            # We have decoded data — go to the mount path. If the
            # prefetch worker (or an earlier load) has already built
            # the flat-path bundle for this (file, query) pair, pass
            # it through so the dispatcher skips the main-thread
            # FileView + strip rebuild entirely.
            query_sig = self._current_query_signature()
            prebuilt = self._prebuilt_cache.get((parent_id, query_sig))
            self._dispatch_preview_mount(parent_id, focus_chunk_seq, chunks, prebuilt=prebuilt)
            return

        # Need to decode first. The bar appears immediately; the worker
        # decodes off-thread and its callback re-enters via the chunk
        # data path.
        self._cancel_preview_mount_task()
        # Keep the previously-active content visible during the decode
        # rather than blanking the pane. The app-level progress strip
        # is the user-visible loading signal, and the debounced cursor-
        # move dispatch means the user has already committed to this
        # file before we land here. The flat-buffer
        # ``_activate_flat_buffer`` / structural ``_activate_preview_container``
        # paths swap visibility atomically once the new content is ready.
        self._show_progress_bar(total=1, phase="decoding…")

        target_parent_id = parent_id
        target_focus = focus_chunk_seq
        searcher = self._searcher
        # Pull the worker count from config so users can tune the
        # decode parallelism via Settings without code edits. 1 = serial.
        decode_workers = (
            self._config.defaults.preview_decode_workers if self._config is not None else 1
        )
        # Estimate the wrap width the eventual ``LineBufferPreview``
        # will be laid out at, so the worker can pre-render Strips at
        # the correct width and avoid a main-thread rewrap on first
        # paint. ``content_size`` excludes the pane's padding; the
        # buffer itself reserves one extra column for its own
        # ``scrollbar-gutter: stable``. If the estimate ends up wrong
        # (e.g. the user resizes the terminal between dispatch and
        # paint) ``_rebuild_after_layout`` will catch it.
        try:
            pane_widget = self.query_one("#preview_pane", VerticalScroll)
            # Floor of 20 — see prefetch _prefetch_top_results for the
            # PDF-single-column rationale.
            measured = pane_widget.content_size.width - 1
            estimated_wrap_width = max(20, measured) if measured > 0 else 0
        except Exception:
            estimated_wrap_width = 0
        app = self

        def _load() -> None:
            try:
                fetched = searcher.get_file_chunks(target_parent_id, max_workers=decode_workers)
            except Exception as e:
                app.call_from_thread(app._on_preview_load_failed, e)
                return
            # For the flat-buffer path (PDF / TXT) the FileView build —
            # which computes per-chunk match spans and stitches the
            # global line buffer — and the per-line ``rich.Console.render``
            # pass are pure-Python data work that easily dominate the
            # main-thread cost on large documents. Do both here in the
            # worker so the UI stays responsive during the decode +
            # assemble phase. Structural formats (md / docx / pptx)
            # skip this path; their mount path is per-chunk.
            prebuilt: RenderedDocument | None = None
            try:
                if fetched and choose_preview_mode(fetched) == "flat":
                    fv = app._build_file_view_for_chunks(fetched)
                    wrap_width = estimated_wrap_width if estimated_wrap_width > 0 else 0
                    prebuilt = build_rendered_document(fv, wrap_width=wrap_width)
            except Exception:
                # Best-effort; fall back to main-thread build inside the dispatcher.
                prebuilt = None
            app.call_from_thread(
                app._on_preview_chunks_loaded,
                target_parent_id,
                target_focus,
                fetched,
                prebuilt,
            )

        _ = asyncio.get_event_loop()  # ensure a loop exists for the callback
        self.run_worker(_load, thread=True, exclusive=True, group="preview-load")

    def _prune_active_to_window(self, margin: int = 3) -> None:
        """Drop the currently-active container's off-screen chunks down to its
        visible window. Used when switching files: the outgoing container stays
        on screen while the incoming one builds, so its full-mounted DOM would
        otherwise inflate the incoming mount's arrange (Option C's inter-file
        cost). Flash-free — the visible window stays put; chunks removed ABOVE
        the viewport are scroll-compensated so the on-screen content doesn't
        shift while the outgoing container is still visible during the swap."""
        import contextlib

        container = self._active_preview
        if container is None:
            return
        window = _VISIBLE_FIRST_ABOVE + _VISIBLE_FIRST_BELOW + 2 * margin + 1
        if len(container.mounted_indices) <= window:
            return  # not enough off-screen DOM to be worth pruning
        try:
            pane = self.query_one("#preview_pane", VerticalScroll)
        except Exception:
            return
        if pane.size.height <= 0:
            return
        chunks = self._chunk_cache.get(container.parent_doc_id)
        if not chunks:
            return
        vtop = float(pane.scroll_y)
        vbot = vtop + float(pane.size.height)
        ranges: list[tuple[int, Widget, float, float]] = []
        for i in sorted(container.mounted_indices):
            if i >= len(chunks):
                # Unreachable today: mounted_indices is built from THIS
                # _chunk_cache list, each file decodes once per query, and a
                # query change clears the chunk AND preview caches together — so
                # a cached container always matches its chunks. Cheap crash-guard
                # only, in case that coupling is ever broken; there is no live
                # stale state here to normalise.
                continue
            seq = chunks[i].chunk_seq
            w = container.chunk_widgets.get(seq)
            if w is None:
                continue
            try:
                vr = w.virtual_region  # type: ignore[attr-defined]
                ranges.append((i, w, float(vr.y), float(vr.y + vr.height)))
            except Exception:
                return  # geometry not ready — skip rather than risk a bad scroll
        visible = [i for (i, _w, y0, y1) in ranges if y1 > vtop and y0 < vbot]
        if not visible:
            return
        keep_lo, keep_hi = min(visible) - margin, max(visible) + margin
        above_height = 0.0
        to_remove: list[tuple[int, Widget]] = []
        for i, w, y0, y1 in ranges:
            if i < keep_lo:
                above_height += y1 - y0
                to_remove.append((i, w))
            elif i > keep_hi:
                to_remove.append((i, w))
        if not to_remove:
            return
        import time as _time

        from fnd.tui import _perf

        _pt0 = _time.perf_counter()
        self.begin_reconcile_scroll()
        try:
            for i, w in to_remove:
                seq = chunks[i].chunk_seq
                # display:none leaves the arrange immediately; remove() then frees
                # it. (Keeping ~1000s of display:none widgets alive is worse — they
                # still get walked by settle and inflate the next mount.)
                with contextlib.suppress(Exception):
                    w.display = False
                with contextlib.suppress(Exception):
                    w.remove()
                container.mounted_indices.discard(i)
                container.chunk_widgets.pop(seq, None)
                container.match_targets.pop(seq, None)
            if above_height > 0:
                with contextlib.suppress(Exception):
                    pane.scroll_to(y=max(0.0, vtop - above_height), animate=False, immediate=True)
        finally:
            self.end_reconcile_scroll()
        _perf.mark("prune", removed=len(to_remove), ms=(_time.perf_counter() - _pt0) * 1000.0)

    def _dispatch_preview_mount(
        self,
        parent_id: str,
        focus_chunk_seq: int,
        chunks: list[FileChunk],
        *,
        prebuilt: RenderedDocument | None = None,
    ) -> None:
        """Route flat vs structural. ``prebuilt`` is a worker-built bundle for
        flat path; structural ignores it."""
        import asyncio

        # Phase 5 redesign: route by format. PDF / TXT take the flat-
        # buffer path (one widget per file, line API, line-precise
        # scrollbar markers). MD / DOCX / PPTX stay on the structural
        # Markdown widget below.
        if choose_preview_mode(chunks) == "flat":
            self._dispatch_flat_buffer_mount(parent_id, focus_chunk_seq, chunks, prebuilt=prebuilt)
            return

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
                # Target already mounted (Option C full-mount makes this the
                # common case for internal jumps). A bare reconcile() scrolls
                # before heavy match geometry is final and lands off-screen, so
                # route through the scoped settle (cheap when already idle).
                import asyncio as _asyncio

                self._preview_mount_task = _asyncio.create_task(
                    self._settled_instant_scroll(container, parent_id, focus_chunk_seq)
                )
                return
            # Same-file, target match OUTSIDE the mounted window. Resuming the
            # SAME container would mount the new window in document order —
            # which, for an upward jump, prepends above the current match and
            # slides the visible content (the "flash wrong content, then land"
            # reflow). Instead build a FRESH container at the new focus and
            # atomic-swap to it, exactly like a between-file nav: the fresh
            # container builds invisibly (mounted below the current one, so no
            # shift), then the swap hides the old and reveals the new at the
            # match in one tick. The old container is dropped from the cache by
            # the fresh one's put() and swept on the next navigation.
            self._cancel_preview_mount_task()
            self._cancel_lazy_mount_task()
            self._hide_progress_bar()
            fresh = PreviewContainer(
                parent_doc_id=parent_id,
                query_signature=query_sig,
                total_chunks=len(chunks),
            )
            self._preview_mount_task = asyncio.create_task(
                self._mount_chunks_async(
                    parent_id,
                    focus_chunk_seq,
                    chunks,
                    fresh,
                )
            )
            return

        self._cancel_preview_mount_task()
        # Option C hardening: the outgoing file may be FULL-mounted (~1000s of
        # widgets). It stays on screen while the incoming file builds, and
        # Textual's arrange scales with total DOM — so a big outgoing container
        # inflates the new file's mount several-fold. Prune it to its visible
        # window now (flash-free) so the incoming mount is cheap.
        self._prune_active_to_window()
        # Sweep stranded containers — but preserve any still being filled by
        # a prefetch task. Removing those would orphan the task and trigger
        # a MountError on its next mount-before call.
        import contextlib as _contextlib

        cached_containers = set(self._preview_cache._cache.values())
        for stranded in list(self.query(PreviewContainer)):
            if stranded in cached_containers:
                continue
            pfetch = getattr(stranded, "_prefetch_task", None)
            if pfetch is not None and not pfetch.done():
                continue
            with _contextlib.suppress(Exception):
                stranded.remove()
        if self._active_preview is not None and self._active_preview not in cached_containers:
            self._active_preview = None
        # Adopt a still-in-flight prefetched container for this key so the
        # cold branch resumes it; _mount_chunks_async cancels its prefetch
        # task first to avoid concurrent-mount races.
        cached = self._preview_cache.get(parent_id, query_sig)
        if cached is None:
            for c in self.query(PreviewContainer):
                if (
                    c.parent_doc_id == parent_id
                    and c.query_signature == query_sig
                    and c not in cached_containers
                ):
                    cached = c
                    break

        import os

        reveal_first = os.environ.get("_FND_REVEAL_FIRST") == "1"
        cache_keys = [f"{pid[:8]}/{sig[:6]}" for (pid, sig) in self._preview_cache._cache]
        dom_keys = [
            f"{c.parent_doc_id[:8]}/{c.query_signature[:6]}"
            f"(t={'a' if getattr(c, '_prefetch_task', None) is not None and not c._prefetch_task.done() else 'd'})"  # pyright: ignore[reportAttributeAccessIssue]
            for c in self.query(PreviewContainer)
        ]
        self._diag_log(
            f"dispatch_preview cache_check parent={parent_id[:8]} sig={query_sig[:6]} "
            f"cached={'yes' if cached is not None else 'no'} "
            f"is_complete={cached.is_complete if cached is not None else None} "
            f"focus_in_widgets={focus_chunk_seq in cached.chunk_widgets if cached is not None else False} "
            f"focus_seq={focus_chunk_seq} reveal_first_env={reveal_first} "
            f"cache_keys={cache_keys} dom_keys={dom_keys}"
        )
        if cached is not None and (
            cached.is_complete or (reveal_first and focus_chunk_seq in cached.chunk_widgets)
        ):
            # Reveal-first: activate visible, scroll on next refresh.
            if reveal_first:
                self._activate_preview_container(cached, pre_reveal=False)
                self._refresh_match_scrollbar(chunks)
                # One-tick scroll: _do_scroll_to_chunk's own retry chain
                # handles any residual region.height==0 race. The prior
                # two-tick wrapping was wasting a refresh tick (~50-200ms
                # depending on DOM size) for every cache-hit click.
                self.call_after_refresh(self._preview_scroll.reconcile)
                if not cached.is_complete:
                    # Resume the partial mount in the background; the
                    # scroll above is canonical so suppress the task's
                    # own scroll attempts.
                    import asyncio as _asyncio

                    self._preview_mount_task = _asyncio.create_task(
                        self._mount_chunks_async(
                            parent_id,
                            focus_chunk_seq,
                            chunks,
                            cached,
                            skip_internal_scrolls=True,
                        )
                    )
                return
            self._activate_preview_container(cached, pre_reveal=True, keep_outgoing=True)
            self._refresh_match_scrollbar(chunks)
            self._show_progress_bar(total=1, progress=0, phase="rendering…")
            self.call_after_refresh(self._finalize_pre_reveal, cached, focus_chunk_seq)
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
            phase="mounting…",
        )
        self._preview_mount_task = asyncio.create_task(
            self._mount_chunks_async(parent_id, focus_chunk_seq, chunks, container)
        )

    def _dispatch_flat_buffer_mount(
        self,
        parent_id: str,
        focus_chunk_seq: int,
        chunks: list[FileChunk],
        *,
        prebuilt: RenderedDocument | None = None,
    ) -> None:
        """Flat-buffer mount: resolve doc (cache > prebuilt > main-thread
        build), install into the shared widget, activate."""
        query_sig = self._current_query_signature()
        cache_key = (parent_id, query_sig)

        doc = self._flat_buffer_cache.get(cache_key)
        cache_hit = doc is not None
        if doc is None:
            doc = prebuilt
        if doc is None:
            try:
                pane_widget = self.query_one("#preview_pane", VerticalScroll)
                measured = pane_widget.content_size.width - 1
                wrap_width = max(20, measured) if measured > 0 else 0
            except Exception:
                wrap_width = 0
            fv = self._build_file_view_for_chunks(chunks)
            doc = build_rendered_document(fv, wrap_width=wrap_width)

        self._flat_buffer_cache[cache_key] = doc
        self._flat_buffer_cache.move_to_end(cache_key)
        while len(self._flat_buffer_cache) > _PREVIEW_CACHE_MAX_FILES:
            self._flat_buffer_cache.popitem(last=False)

        # A post-query auto-park can arrive pointing at a chunk that BM25
        # matched but carries no highlightable span (e.g. a tree-rebuild
        # cursor echo lands on chunk 0). Scrolling there parks the view at
        # the chunk's top with nothing highlighted, and a second racing
        # dispatch then clobbers the correct match scroll — last writer
        # wins, non-deterministically. Resolve to the file's first matching
        # chunk so every dispatch for this (file, query) lands on the same
        # match regardless of arrival order. Genuine match chunks (real
        # section navigation) and the no-match browse case are left as-is.
        if doc.fv.first_hit_line_in_chunk and focus_chunk_seq not in doc.fv.first_hit_line_in_chunk:
            focus_chunk_seq = min(doc.fv.first_hit_line_in_chunk)

        buf = self._ensure_shared_flat_buffer()
        if self._installed_flat_key != cache_key:
            # New doc: install + synchronous no-flash scroll to the match.
            self._install_flat_doc(
                buf,
                doc,
                focus_chunk_seq,
                parent_id=parent_id,
                context_fraction=_MATCH_CONTEXT_FRACTION,
            )
            self._installed_flat_key = cache_key
        self._activate_flat_buffer(buf)
        # Route the flat match scroll through the controller: arm with the
        # resolved focus chunk and reconcile (idempotent — re-applies the
        # install's scroll; for intra-file nav it IS the scroll). The 25%
        # context margin matches the structural path.
        self._preview_scroll.arm(ScrollAnchor(parent_id, focus_chunk_seq))
        self._preview_scroll.reconcile()
        self._diag_log(
            f"dispatch_flat parent={parent_id[:8]} cache_hit={'yes' if cache_hit else 'no'} "
            f"prebuilt={'yes' if prebuilt is not None else 'no'} strips={len(doc.strips)} "
            f"wrap_width={doc.wrap_width} chunks={len(chunks)}"
        )
        self._hide_progress_bar()
        self._preview_parent_id = parent_id
        self._refresh_status()

    def _ensure_shared_flat_buffer(self) -> LineBufferPreview:
        return self._flat.ensure_shared_buffer()

    def _install_flat_doc(
        self,
        buf: LineBufferPreview,
        doc: RenderedDocument,
        focus_chunk_seq: int,
        *,
        parent_id: str,
        context_fraction: float = 0.0,
    ) -> None:
        self._flat.install_doc(
            buf, doc, focus_chunk_seq, parent_id=parent_id, context_fraction=context_fraction
        )

    def _reset_shared_flat_buffer(self) -> None:
        self._flat.reset()

    @staticmethod
    def _focus_line_for_chunk(fv: FileView, chunk_id: int) -> int | None:
        return FlatBufferView.focus_line_for_chunk(fv, chunk_id)

    def _build_file_view_for_chunks(self, chunks: list[FileChunk]) -> FileView:
        return self._flat.build_file_view(chunks)

    def _activate_flat_buffer(self, buf: LineBufferPreview) -> None:
        self._flat.activate(buf)

    # ── Flat-buffer delegation (state lives on FlatBufferView) ────
    # Tests and the preview/scroll code read AND write these names on
    # the app; the property pairs keep that surface stable while the
    # view owns the state.

    @property
    def _flat_buffer_cache(self) -> OrderedDict[tuple[str, str], RenderedDocument]:
        return self._flat.cache

    @_flat_buffer_cache.setter
    def _flat_buffer_cache(self, value: OrderedDict[tuple[str, str], RenderedDocument]) -> None:
        self._flat.cache = value

    @property
    def _active_flat_buffer(self) -> LineBufferPreview | None:
        return self._flat.active_buffer

    @_active_flat_buffer.setter
    def _active_flat_buffer(self, value: LineBufferPreview | None) -> None:
        self._flat.active_buffer = value

    @property
    def _shared_flat_buffer(self) -> LineBufferPreview | None:
        return self._flat.shared_buffer

    @_shared_flat_buffer.setter
    def _shared_flat_buffer(self, value: LineBufferPreview | None) -> None:
        self._flat.shared_buffer = value

    @property
    def _installed_flat_key(self) -> tuple[str, str] | None:
        return self._flat.installed_key

    @_installed_flat_key.setter
    def _installed_flat_key(self, value: tuple[str, str] | None) -> None:
        self._flat.installed_key = value

    def _current_query_signature(self) -> str:
        return self._search.query_signature()

    def _show_progress_bar(
        self,
        *,
        total: int | None,
        progress: int = 0,
        phase: str | None = None,
    ) -> None:
        """Open or update the progress session for a preview load. Determinate
        only — ``total=None`` is treated as ``total=1`` so the indeterminate
        red pulse never paints."""
        total_eff = total if (total is not None and total > 0) else 1
        s = self._progress.active
        if s is None or s.closed:
            s = self._progress.open(phase or "loading…", total=total_eff)
        else:
            if phase is not None:
                s.set_phase(phase)
            s.set_total(total_eff)
        s.set_progress(progress)
        import contextlib

        # Pane's own scrollbar would jitter as virtual_size grows; the strip
        # below the layout carries the loading signal instead.
        with contextlib.suppress(Exception):
            self.query_one("#preview_pane", VerticalScroll).add_class("is-loading")

    def _hide_progress_bar(self) -> None:
        """Close the active session + re-enable pane scrolling. Idempotent."""
        s = self._progress.active
        if s is not None and not s.closed:
            s.close()
        import contextlib

        with contextlib.suppress(Exception):
            self.query_one("#preview_pane", VerticalScroll).remove_class("is-loading")

    def _update_progress_bar(self, progress: int) -> None:
        s = self._progress.active
        if s is not None and not s.closed:
            s.set_progress(progress)

    def _clear_pane_placeholder(self) -> None:
        """Drop the empty-state Static. Called by every activate path so the
        placeholder never paints above a real preview."""
        import contextlib

        with contextlib.suppress(Exception):
            pane = self.query_one("#preview_pane", VerticalScroll)
            for w in list(pane.children):
                if isinstance(w, Static) and w.id == "placeholder":
                    with contextlib.suppress(Exception):
                        w.remove()

    def _activate_preview_container(
        self,
        container: PreviewContainer,
        *,
        pre_reveal: bool = False,
        keep_outgoing: bool = False,
    ) -> None:
        """Make ``container`` the only visible preview. With
        ``pre_reveal=True`` the container is laid out but invisible
        (opacity:0) until the scroll lands — no flash to file-top before
        the jump-to-match. With ``keep_outgoing=True`` the previously-active
        container stays visible (so the pane never blanks) until the atomic
        reveal swaps to ``container`` (see :meth:`_swap_reveal_target`)."""
        from fnd.tui import _perf

        self._clear_pane_placeholder()
        # Hold the outgoing preview on screen while the incoming one builds
        # invisibly; the reveal swap hides it and shows the new one in one tick.
        # Only keep a genuinely-visible prior container (not one left invisible
        # by a superseded mount) — otherwise the pane would blank anyway.
        prior = self._active_preview
        outgoing = (
            prior
            if keep_outgoing
            and pre_reveal
            and prior is not None
            and prior is not container
            and not prior.has_class("-pre-reveal")
            and not prior.has_class("-hidden")
            else None
        )
        self._outgoing_preview = outgoing
        for child in self.query(PreviewContainer):
            if child is container:
                child.remove_class("-hidden")
                if pre_reveal:
                    child.add_class("-pre-reveal")
                else:
                    child.remove_class("-pre-reveal")
            elif child is outgoing:
                # Keep visible until the swap; don't disturb its scroll.
                child.remove_class("-hidden")
                child.remove_class("-pre-reveal")
            else:
                child.add_class("-hidden")
                child.remove_class("-pre-reveal")
        for child in self.query(LineBufferPreview):
            child.add_class("-hidden")
        self._active_preview = container
        self._active_flat_buffer = None
        if not pre_reveal:
            _perf.mark(
                "click_to_display_end",
                parent_id=container.parent_doc_id,
                path="structural_immediate",
            )
        self._preview_parent_id = container.parent_doc_id
        self._chunk_widgets = container.chunk_widgets
        self._match_targets = container.match_targets
        # Cache-hit paths return without _mount_chunks_async (which is
        # where _refresh_status normally fires at the end); refresh here
        # so the pane title swaps to the activated file immediately.
        self._refresh_status()

    def swap_reveal_target(self, target: Widget, margin: int) -> bool:
        """Atomic preview swap: hide the outgoing container, position the
        incoming one so ``target`` sits ``margin`` rows down, and reveal it —
        all in one tick. Returns True when a swap happened, False when there is
        no outgoing container (the caller then scrolls + reveals normally).

        The outgoing container stayed on screen through the whole build, so the
        first frame the user sees after this is the new preview already at its
        match — no blank, no scroll-into-place. ``target``'s offset is taken
        relative to the incoming container's top, which is scroll-independent
        and so survives the outgoing container leaving the layout."""
        outgoing = self._outgoing_preview
        new = self._active_preview
        if outgoing is None or new is None or outgoing is new:
            return False
        offset = target.region.y - new.region.y
        target_y = max(0, offset - margin)
        pane = self.query_one("#preview_pane", VerticalScroll)
        outgoing.add_class("-hidden")
        pane.scroll_to(y=target_y, animate=False, immediate=True)
        new.remove_class("-pre-reveal")
        self._outgoing_preview = None
        return True

    def _reveal_preview(self, container: PreviewContainer) -> None:
        """Reveal ``container`` and drop any still-held outgoing preview.
        Fallback for paths where :meth:`swap_reveal_target` did not run (no
        match resolved, or no outgoing) — a no-op for the class already lifted
        by the swap.

        Guard: a finalize/reveal callback is queued via ``call_after_refresh``
        and runs a tick later. If a newer navigation superseded this mount in
        the meantime, ``container`` is no longer ``_active_preview`` — revealing
        it would surface the wrong file and clobber the new nav's outgoing
        reference. Detached finalize tasks aren't cancelled, so this staleness
        check (not task cancellation) is the single point that makes a
        superseded reveal a no-op."""
        if container is not self._active_preview:
            return
        outgoing = self._outgoing_preview
        if outgoing is not None and outgoing is not container:
            outgoing.add_class("-hidden")
        self._outgoing_preview = None
        container.remove_class("-pre-reveal")

    def _finalize_pre_reveal(self, container: PreviewContainer, focus_chunk_seq: int) -> None:
        """Lift ``-pre-reveal`` once focused chunk's compose is ready, then scroll."""
        import time

        t0 = time.perf_counter()
        self._diag_log(
            f"finalize_pre_reveal start seq={focus_chunk_seq} parent_id={container.parent_doc_id}"
        )

        self._do_finalize_pre_reveal(container, focus_chunk_seq, retries=10, t0=t0)

    async def _finalize_via_lock(
        self,
        container: PreviewContainer,
        focus_chunk_seq: int,
        t0: float,
        *,
        expected_above_seqs: list[int] | None = None,
        path: str = "cold_via_lock",
    ) -> None:
        """Wait for *every* chunk above the focus in the mounted window
        to finish building before revealing + scrolling. Awaiting only
        the focus chunk's ``build_done`` (the previous behaviour) let
        the scroll land while siblings above were still height=0; once
        those grew, the focus chunk's virtual_y shifted and the user
        saw the correct match flash, then jump to an unrelated area.
        Waiting for the above-siblings means the focus chunk's
        virtual_y is final at scroll time."""
        import asyncio
        import time

        from fnd.tui import _perf

        _fin_t0 = time.perf_counter()
        header = container.chunk_widgets.get(focus_chunk_seq)
        # Step 1: wait for the focus chunk's build.
        try:
            async with asyncio.timeout(8.0):
                if isinstance(header, FNDMarkdown):
                    await header.build_done.wait()
        except TimeoutError:
            self._diag_log(
                f"finalize_via_lock focus build_done timeout seq={focus_chunk_seq} path={path}"
            )
        # Step 2: wait for the above-window chunks to be MOUNTED, then built.
        # We cannot just read chunk_widgets now: when the focus chunk was
        # prefetched its build_done is already set, so Step 1 returns before
        # Phase 1b has mounted the window — chunk_widgets would hold only the
        # focus chunk (above_waited=0), the scroll would land against a
        # focus-at-top layout, and the view would settle-scroll once the real
        # above content mounts. Yield until every expected above seq exists.
        expected = [s for s in (expected_above_seqs or []) if s < focus_chunk_seq]
        try:
            async with asyncio.timeout(8.0):
                while not all(s in container.chunk_widgets for s in expected):
                    await asyncio.sleep(0)
        except TimeoutError:
            self._diag_log(
                f"finalize_via_lock above mount timeout seq={focus_chunk_seq} "
                f"expected={len(expected)} path={path}"
            )
        above_widgets: list[FNDMarkdown] = [
            w
            for seq, w in container.chunk_widgets.items()
            if seq < focus_chunk_seq and isinstance(w, FNDMarkdown)
        ]
        if above_widgets:
            try:
                async with asyncio.timeout(8.0):
                    await asyncio.gather(*(w.build_done.wait() for w in above_widgets))
            except TimeoutError:
                self._diag_log(
                    f"finalize_via_lock above build_done timeout "
                    f"seq={focus_chunk_seq} above_count={len(above_widgets)} path={path}"
                )
        # Wait for the screen to FULLY settle before scrolling. build_done only
        # says the markdown rendered; the compositor's arrange (which fixes every
        # chunk's region AND the pane's scroll extent) runs over several more
        # refreshes. The old region.height>0 poll only checked the focus chunk
        # and raced the chunks above it still flowing — so a deep match scrolled
        # against a half-settled layout and clamped off-screen. _await_preview_settled
        # is Textual's own message-drain signal (what Pilot waits on): it returns
        # only once every widget has processed its pending layout, so the geometry
        # the scroll reads is final.
        _perf.mark(
            "finalize_buildwait",
            ms=(time.perf_counter() - _fin_t0) * 1000.0,
            above=len(above_widgets),
            path=path,
        )
        import os as _os

        # Option B: scoped settle is the default — wait only on the geometry the
        # scroll reads (focus + above-window heights), not a full-pane drain.
        # _FND_FULL_SETTLE=1 restores the old behaviour as an escape hatch.
        if _os.environ.get("_FND_FULL_SETTLE") == "1":
            await self._await_preview_settled()
        else:
            await self._await_match_settled(header, above_widgets)
        wait_ms = (time.perf_counter() - t0) * 1000
        self._hide_progress_bar()
        _perf.mark(
            "click_to_display_end",
            parent_id=container.parent_doc_id,
            focus_seq=focus_chunk_seq,
            path=path,
        )

        def _reveal_when_landed() -> None:
            self._reveal_preview(container)

        # Scroll while the container is still invisible (opacity:0), then reveal
        # once it lands — so the match never flashes at the file top first. The
        # layout is settled, so this is a single deterministic scroll.
        self._preview_scroll.reconcile(_reveal_when_landed)
        # This render has settled — release the in-flight coalescing
        # latch so a later genuine re-render of the same target can run.
        self._inflight_preview_target = None
        self._diag_log(
            f"finalize_via_lock done seq={focus_chunk_seq} path={path} "
            f"wait_ms={wait_ms:.1f} above_waited={len(above_widgets)}"
        )

    def _do_finalize_pre_reveal(
        self,
        container: PreviewContainer,
        focus_chunk_seq: int,
        retries: int,
        t0: float,
    ) -> None:
        import time

        from fnd.tui import _perf

        header = container.chunk_widgets.get(focus_chunk_seq)
        compose_done = True
        if header is not None and hasattr(header, "first_match_block"):
            compose_done = header.first_match_block is not None  # pyright: ignore[reportAttributeAccessIssue]
        if not compose_done and retries > 0:
            self.call_after_refresh(
                self._do_finalize_pre_reveal,
                container,
                focus_chunk_seq,
                retries - 1,
                t0,
            )
            return

        wait_ms = (time.perf_counter() - t0) * 1000
        self._hide_progress_bar()
        _perf.mark(
            "click_to_display_end",
            parent_id=container.parent_doc_id,
            focus_seq=focus_chunk_seq,
            path="warm_pre_reveal",
        )

        def _reveal_when_landed() -> None:
            self._reveal_preview(container)
            self._diag_log(
                f"finalize_pre_reveal done seq={focus_chunk_seq} "
                f"wait_ms={wait_ms:.1f} elapsed_ms={(time.perf_counter() - t0) * 1000:.1f} "
                f"compose_done={compose_done}"
            )

        # Wait for the screen to fully settle, THEN scroll once + reveal — same
        # deterministic settle the cold path uses (see _finalize_via_lock). The
        # warm reveal is sync, so run the await in a task.
        import asyncio as _asyncio

        async def _settled_reconcile() -> None:
            await self._await_preview_settled()
            self._preview_scroll.reconcile(_reveal_when_landed)

        # Cancel a prior settle-await on this container before replacing it — a
        # rapid re-nav would otherwise leave it running, burning a full DOM-drain
        # and a redundant (generation-guarded) reconcile. Safe to cancel: this
        # task does no cleanup, so CancelledError just unwinds the await. Held on
        # the container so GC can't collect the new one mid-await (RUF006).
        _prior = getattr(container, "_finalize_task", None)
        if _prior is not None and not _prior.done():
            _prior.cancel()
        container._finalize_task = _asyncio.create_task(_settled_reconcile())  # type: ignore[attr-defined]

    async def _await_preview_settled(self, max_rounds: int = 10) -> None:
        """Deterministically wait until the screen has processed all pending
        layout messages, so the preview geometry is final before we scroll.

        Drain = Textual's own settle mechanism (what ``Pilot.pause`` /
        ``_wait_for_screen`` use): schedule a callback on every widget via
        ``call_later`` and wait for them all to fire — i.e. every widget has
        processed the messages queued now. One drain settles the current wave;
        the reflow it triggers posts a follow-up wave, so loop until the screen
        reports no pending layout/repaint/recompose (its ``_on_idle`` condition),
        bounded by ``max_rounds``. Replaces stability-polling heuristics, which
        can't tell a settled layout from a mid-reflow plateau."""
        import asyncio
        import time as _time

        from fnd.tui import _perf

        _t0 = _time.perf_counter()
        rounds = 0
        walked = 0
        reason = "max_rounds"
        try:
            for _ in range(max_rounds):
                try:
                    screen = self.screen
                except Exception:
                    reason = "no_screen"
                    return
                # Drain only what bears on the preview's geometry: the app + screen
                # (which run the arrange) and the preview pane's own subtree — NOT the
                # whole screen (results tree, sidebars), which is irrelevant here and
                # makes the per-round callback count scale with the unrelated DOM.
                try:
                    pane = self.query_one("#preview_pane", VerticalScroll)
                    children = [self, screen, *pane.walk_children(with_self=True)]
                except Exception:
                    # No pane yet — fall back to the screen-wide drain.
                    children = [self, *screen.walk_children(with_self=True)]
                count = 0
                done = asyncio.Event()

                def _dec(_done: asyncio.Event = done) -> None:
                    nonlocal count
                    count -= 1
                    if count == 0:
                        _done.set()

                for child in children:
                    if child.call_later(_dec):
                        count += 1
                rounds += 1
                walked = len(children)
                if count:
                    try:
                        async with asyncio.timeout(5.0):
                            await done.wait()
                    except TimeoutError:
                        reason = "timeout"
                        return
                # Stop once the screen has no pending layout work — the geometry is
                # now final. (These are the flags Screen._on_idle itself checks.)
                if not (
                    getattr(screen, "_layout_required", False)
                    or getattr(screen, "_repaint_required", False)
                    or getattr(screen, "_recompose_required", False)
                    or getattr(screen, "_dirty_widgets", None)
                ):
                    reason = "settled"
                    return
        finally:
            _perf.mark(
                "settle",
                rounds=rounds,
                walked=walked,
                ms=(_time.perf_counter() - _t0) * 1000.0,
                reason=reason,
            )

    async def _await_match_settled(
        self,
        header: FNDMarkdown | Widget | None,
        above_widgets: list[FNDMarkdown],
        max_rounds: int = 12,
    ) -> None:
        """Option B — targeted settle. The full-pane drain waits for the WHOLE
        screen to stop reflowing; but the only geometry the scroll reads is the
        focus chunk's virtual_y, which is fixed once the above-window chunk
        heights stop changing. So drain only [app, screen, focus, above] and
        exit when those heights are stable for two consecutive rounds — far
        fewer callbacks/round than walking every block in the pane, and an
        earlier exit than the screen-global flags allow. Stability is judged on
        the SPECIFIC heights that move the match, not a generic region poll, so
        a mid-reflow plateau can't masquerade as settled (the heights are still
        changing during reflow)."""
        import asyncio
        import time as _time

        from fnd.tui import _perf

        _t0 = _time.perf_counter()
        watch: list[Widget] = [w for w in [header, *above_widgets] if w is not None]
        # Nothing to track (no focus/above widgets, or no resolvable match) —
        # fall back to the full-pane drain rather than scroll against unknown
        # geometry. The scoped path only buys us anything when there ARE heights
        # to watch settle.
        if not watch:
            await self._await_preview_settled()
            return
        try:
            screen = self.screen
        except Exception:
            return  # no screen (teardown / transition) — nothing to settle
        targets = [self, screen, *watch]  # App + Screen + widgets all have call_later

        def _sig() -> tuple[int, ...]:
            out: list[int] = []
            for w in watch:
                try:
                    out.append(w.size.height)
                except Exception:
                    out.append(-1)
            return tuple(out)

        prev: tuple[int, ...] | None = None
        stable = 0
        rounds = 0
        reason = "max_rounds"
        try:
            for _ in range(max_rounds):
                count = 0
                done = asyncio.Event()

                def _dec(_done: asyncio.Event = done) -> None:
                    nonlocal count
                    count -= 1
                    if count == 0:
                        _done.set()

                for w in targets:
                    if w.call_later(_dec):
                        count += 1
                rounds += 1
                if count:
                    try:
                        async with asyncio.timeout(5.0):
                            await done.wait()
                    except TimeoutError:
                        reason = "timeout"
                        return
                cur = _sig()
                # All watched heights must be real (>0) AND unchanged twice.
                if cur == prev and all(h > 0 for h in cur):
                    stable += 1
                    if stable >= 2:
                        reason = "stable"
                        return
                else:
                    stable = 0
                prev = cur
        finally:
            _perf.mark(
                "settle",
                rounds=rounds,
                walked=len(targets),
                ms=(_time.perf_counter() - _t0) * 1000.0,
                reason=reason,
                scoped=True,
            )

    async def _settled_instant_scroll(
        self, container: PreviewContainer, parent_id: str, focus_chunk_seq: int
    ) -> None:
        """Option C: the target chunk is already mounted, so scroll straight to
        it — but settle the focus + nearest-above heights first (cheap, ~2 rounds
        when the file is idle) so heavy table/fence geometry is final and the
        match lands on-screen instead of clamping off."""
        from fnd.tui import _perf

        header = container.chunk_widgets.get(focus_chunk_seq)
        above_seqs = sorted(s for s in container.chunk_widgets if s < focus_chunk_seq)[-7:]
        above = [
            w for s in above_seqs if isinstance((w := container.chunk_widgets.get(s)), FNDMarkdown)
        ]
        await self._await_match_settled(header, above)
        _perf.mark("click_to_display_end", parent_id=parent_id, path="already_active_scroll_only")
        self._preview_scroll.reconcile()

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

    def _cancel_lazy_mount_task(self) -> None:
        """Drop any in-flight scroll-driven mount task. Called on file
        switch / query change so the task can't mount stale chunks into
        a container the user has moved away from."""
        import contextlib

        if self._lazy_mount_check_timer is not None:
            with contextlib.suppress(Exception):
                self._lazy_mount_check_timer.stop()  # type: ignore[attr-defined]
            self._lazy_mount_check_timer = None
        task = self._lazy_mount_task
        if task is None:
            return
        try:
            done = task.done()  # type: ignore[attr-defined]
        except Exception:
            done = True
        if not done:
            with contextlib.suppress(Exception):
                task.cancel()  # type: ignore[attr-defined]
        self._lazy_mount_task = None

    async def _cancel_prefetch_task_on(self, container: PreviewContainer) -> None:
        """Cancel + await any background prefetch task on ``container``
        so the user-side mount doesn't race it and trip MountError."""
        import asyncio
        import contextlib

        task = getattr(container, "_prefetch_task", None)
        if task is None:
            return
        try:
            if task.done():
                container._prefetch_task = None  # type: ignore[attr-defined]
                return
        except Exception:
            container._prefetch_task = None  # type: ignore[attr-defined]
            return
        task.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
        container._prefetch_task = None  # type: ignore[attr-defined]

    def _on_preview_load_failed(self, exc: BaseException) -> None:
        """Worker error callback. Hide the bar, surface a notify."""
        self._hide_progress_bar()
        self.notify(f"Preview load failed: {exc}", severity="error")

    def _on_preview_chunks_loaded(
        self,
        parent_id: str,
        focus_chunk_seq: int,
        chunks: list[FileChunk],
        prebuilt: RenderedDocument | None = None,
    ) -> None:
        """Worker callback. Caches chunks + (optional) flat-path bundle;
        re-enters the mount path."""
        self._chunk_cache[parent_id] = chunks
        if prebuilt is not None:
            # Cache the bundle so a later visit to the same file in the
            # same query can install it without re-decoding or re-
            # rendering. Same key as ``_flat_buffer_cache``.
            self._prebuilt_cache[(parent_id, self._current_query_signature())] = prebuilt
        if not chunks:
            # Empty file — hide bar, leave pane blank.
            self._hide_progress_bar()
            self._preview_parent_id = parent_id
            self._refresh_status()
            return
        self._dispatch_preview_mount(parent_id, focus_chunk_seq, chunks, prebuilt=prebuilt)

    def _prefetch_top_results(self, *, anchor_parent_id: str | None = None) -> None:
        """Decode + pre-mount widgets for an N-result window so cursor moves
        land on cache hits. ``preview_prefetch_count`` = N; 0 disables.
        Parallelism bounded by ``preview_decode_workers``.

        ``anchor_parent_id`` centres the window around the cursor's position
        in the result list instead of starting from the top — lets the
        buffer follow the user when they navigate past the initial range.
        """
        # Discard mount jobs queued for the previous anchor — stale
        # work would otherwise keep the drainer (and the asyncio loop)
        # busy across navigation.
        q = self._prefetch_sink_queue
        if q is not None:
            import contextlib as _contextlib

            drained = 0
            while True:
                try:
                    q.get_nowait()
                except Exception:
                    break
                with _contextlib.suppress(Exception):
                    q.task_done()
                drained += 1
            if drained:
                self._diag_log(f"prefetch_top drained_stale_jobs={drained}")
        if self._searcher is None or not self._groups:
            return
        if self._config is not None:
            n = self._config.defaults.preview_prefetch_count
        else:
            from fnd.config import Defaults

            n = Defaults().preview_prefetch_count
        if n <= 0:
            return
        # Build the candidate window. With no anchor we walk from rank 0;
        # with an anchor we start ~N/2 above its position so we cover both
        # directions of likely navigation.
        start_idx = 0
        if anchor_parent_id is not None:
            anchor_idx = next(
                (i for i, g in enumerate(self._groups) if g.parent_id == anchor_parent_id),
                -1,
            )
            if anchor_idx >= 0:
                half = max(1, n // 2)
                start_idx = max(0, anchor_idx - half)
        targets: list[tuple[str, int]] = []
        seen: set[str] = set()
        already_cached: list[str] = []
        query_sig_for_filter = self._current_query_signature()
        for g in self._groups[start_idx:]:
            if g.parent_id in seen:
                continue
            seen.add(g.parent_id)
            # Filter by preview_cache (widget tree ready), not chunk_cache:
            # a file whose chunks are cached but whose mount got drained
            # by a prior cursor move must be re-queued. Also skip if it's
            # the active preview — that one's owned by the user-side path.
            in_preview = self._preview_cache.get(g.parent_id, query_sig_for_filter) is not None
            is_active = (
                self._active_preview is not None
                and self._active_preview.parent_doc_id == g.parent_id
                and self._active_preview.query_signature == query_sig_for_filter
            )
            if in_preview or is_active:
                already_cached.append(g.parent_id[:8])
                continue
            focus = g.hits[0].chunk_seq if g.hits else 0
            targets.append((g.parent_id, focus))
            if len(targets) >= n:
                break
        self._diag_log(
            f"prefetch_top n={n} anchor={anchor_parent_id[:8] if anchor_parent_id else None} "
            f"start_idx={start_idx} targets={[t[0][:8] for t in targets]} "
            f"already_cached={already_cached}"
        )
        if not targets:
            return

        searcher = self._searcher
        decode_workers = (
            self._config.defaults.preview_decode_workers if self._config is not None else 1
        )
        try:
            pane_widget = self.query_one("#preview_pane", VerticalScroll)
            # Floor of 20 mirrors the cold-load + md-flat paths. Without
            # it, if the pane isn't laid out at prefetch time (content
            # width 0–1) every flat-path file gets pre-rendered to
            # 1-cell strips and paints as a single vertical column on
            # first reveal — the "PDF only shows a single line" symptom.
            measured = pane_widget.content_size.width - 1
            estimated_wrap_width = max(20, measured) if measured > 0 else 0
        except Exception:
            estimated_wrap_width = 0
        query_sig = self._current_query_signature()
        app = self

        def _prefetch_one(parent_id: str, focus_seq: int) -> None:
            import time as _time

            t0 = _time.perf_counter()
            # Reuse cached chunk data if present — only the mount got dropped,
            # not the decode. Avoids re-running PDF/docx extraction.
            cached_chunks = app._chunk_cache.get(parent_id)
            if cached_chunks is not None:
                fetched = cached_chunks
                decode_ms = 0.0
            else:
                try:
                    fetched = searcher.get_file_chunks(parent_id, max_workers=decode_workers)
                except Exception:
                    app.call_from_thread(
                        app._diag_log,
                        f"prefetch_one decode FAILED parent={parent_id[:8]}",
                    )
                    return
                decode_ms = (_time.perf_counter() - t0) * 1000.0
            # Stale-query guard: if the user has moved on, drop the
            # work without scheduling any main-thread sinks.
            if query_sig != app._current_query_signature():
                app.call_from_thread(
                    app._diag_log,
                    f"prefetch_one stale parent={parent_id[:8]} decode_ms={decode_ms:.0f}",
                )
                return
            app.call_from_thread(app._record_prefetched_chunks, parent_id, fetched)
            if not fetched:
                return
            mode = choose_preview_mode(fetched)
            app.call_from_thread(
                app._diag_log,
                f"prefetch_one done parent={parent_id[:8]} decode_ms={decode_ms:.0f} "
                f"chunks={len(fetched)} mode={mode} focus_seq={focus_seq}",
            )
            if mode == "flat":
                try:
                    fv = app._build_file_view_for_chunks(fetched)
                    wrap_width = estimated_wrap_width if estimated_wrap_width > 0 else 0
                    doc = build_rendered_document(fv, wrap_width=wrap_width)
                except Exception:
                    return
                app.call_from_thread(app._record_prefetched_bundle, parent_id, query_sig, doc)
                app.call_from_thread(app._prefetch_mount_flat, parent_id, query_sig, doc, focus_seq)
            else:
                app.call_from_thread(
                    app._prefetch_mount_structural,
                    parent_id,
                    query_sig,
                    list(fetched),
                    focus_seq,
                )

        def _prefetch() -> None:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            workers = max(1, decode_workers)
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = [ex.submit(_prefetch_one, pid, focus) for pid, focus in targets]
                for f in as_completed(futures):
                    # Drop everything on query change — _run_query has cleared
                    # caches and any in-flight work here is stale.
                    if query_sig != app._current_query_signature():
                        for other in futures:
                            other.cancel()
                        return
                    with contextlib.suppress(Exception):
                        f.result()

        self.run_worker(
            _prefetch,
            thread=True,
            exclusive=True,
            group="preview-prefetch",
            description="prefetching top-N preview bundles",
        )

    def _record_prefetched_chunks(self, parent_id: str, chunks: list[FileChunk]) -> None:
        """Main-thread sink for prefetch worker chunk results. Stored
        only if not already present so a concurrent user-initiated
        load (which would have richer state) wins."""
        if parent_id not in self._chunk_cache:
            self._chunk_cache[parent_id] = chunks

    def _record_prefetched_bundle(
        self,
        parent_id: str,
        query_sig: str,
        doc: RenderedDocument,
    ) -> None:
        """Stash a worker-built bundle if the query is still current."""
        if query_sig != self._current_query_signature():
            return
        self._prebuilt_cache[(parent_id, query_sig)] = doc

    def _prefetch_mount_flat(
        self,
        parent_id: str,
        query_sig: str,
        doc: RenderedDocument,
        focus_chunk_seq: int,
    ) -> None:
        """Queue a hidden flat-buffer pre-mount; drainer runs it serially."""
        q = self._prefetch_sink_queue
        if q is None:
            return
        if query_sig != self._current_query_signature():
            return

        async def _job() -> None:
            await self._prefetch_mount_flat_async(parent_id, query_sig, doc, focus_chunk_seq)

        q.put_nowait(_job)

    async def _prefetch_mount_flat_async(
        self,
        parent_id: str,
        query_sig: str,
        doc: RenderedDocument,
        focus_chunk_seq: int,
    ) -> None:
        """Stash the prefetched RenderedDocument in the value cache. No mount —
        user activation installs into the shared widget on click."""
        _ = focus_chunk_seq  # focus is recomputed at install time
        if query_sig != self._current_query_signature():
            return
        cache_key = (parent_id, query_sig)
        if cache_key in self._flat_buffer_cache:
            return
        self._flat_buffer_cache[cache_key] = doc
        self._flat_buffer_cache.move_to_end(cache_key)
        while len(self._flat_buffer_cache) > _PREVIEW_CACHE_MAX_FILES:
            self._flat_buffer_cache.popitem(last=False)

    def _prefetch_mount_structural(
        self,
        parent_id: str,
        query_sig: str,
        chunks: list[FileChunk],
        focus_chunk_seq: int,
    ) -> None:
        """Queue a hidden structural pre-mount so cached clicks land
        as a visibility flip. Safe to default-on now that W3 collapses
        per-cell widgets — see bench_input_lag for the DOM-size
        breakdown. Opt out with _FND_NO_PREMOUNT=1."""
        import os as _os

        if _os.environ.get("_FND_NO_PREMOUNT") == "1":
            return
        q = self._prefetch_sink_queue
        if q is None:
            self._diag_log(f"prefetch_mount_structural SKIPPED no-queue parent={parent_id[:8]}")
            return
        if query_sig != self._current_query_signature():
            self._diag_log(f"prefetch_mount_structural SKIPPED stale-sig parent={parent_id[:8]}")
            return
        self._diag_log(
            f"prefetch_mount_structural QUEUED parent={parent_id[:8]} "
            f"focus={focus_chunk_seq} qsize_before={q.qsize()}"
        )

        async def _job() -> None:
            await self._prefetch_mount_structural_async(
                parent_id, query_sig, chunks, focus_chunk_seq
            )

        q.put_nowait(_job)

    async def _prefetch_mount_structural_async(
        self,
        parent_id: str,
        query_sig: str,
        chunks: list[FileChunk],
        focus_chunk_seq: int,
    ) -> None:
        if query_sig != self._current_query_signature():
            self._diag_log(
                f"prefetch_mount_structural_async SKIPPED stale-sig parent={parent_id[:8]}"
            )
            return
        if self._preview_cache.get(parent_id, query_sig) is not None:
            self._diag_log(
                f"prefetch_mount_structural_async SKIPPED already-cached parent={parent_id[:8]}"
            )
            return
        if (
            self._active_preview is not None
            and self._active_preview.parent_doc_id == parent_id
            and self._active_preview.query_signature == query_sig
        ):
            self._diag_log(
                f"prefetch_mount_structural_async SKIPPED already-active parent={parent_id[:8]}"
            )
            return
        import asyncio
        import contextlib

        try:
            pane = self.query_one("#preview_pane", VerticalScroll)
        except Exception:
            self._diag_log(
                f"prefetch_mount_structural_async SKIPPED no-pane parent={parent_id[:8]}"
            )
            return
        self._diag_log(f"prefetch_mount_structural_async STARTING parent={parent_id[:8]}")
        container = PreviewContainer(
            parent_doc_id=parent_id,
            query_signature=query_sig,
            total_chunks=len(chunks),
        )
        container.add_class("-hidden")
        mount_awaitable: object | None = None
        with contextlib.suppress(Exception):
            mount_awaitable = pane.mount(container)
        if mount_awaitable is not None:
            with contextlib.suppress(Exception):
                await mount_awaitable  # type: ignore[misc]

        sub_task = asyncio.create_task(
            self._prefetch_mount_chunk_loop(
                parent_id, query_sig, focus_chunk_seq, chunks, container
            )
        )
        # Exposing the sub-task as _prefetch_task lets the user-side adopt
        # branch (_cancel_prefetch_task_on) await its cancellation cleanly.
        container._prefetch_task = sub_task  # type: ignore[attr-defined]
        try:
            await sub_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _prefetch_mount_chunk_loop(
        self,
        parent_id: str,
        query_sig: str,
        focus_chunk_seq: int,
        chunks: list[FileChunk],
        container: PreviewContainer,
    ) -> None:
        import asyncio
        import contextlib

        from fnd.tui import _perf

        focus_idx = next(
            (i for i, c in enumerate(chunks) if c.chunk_seq == focus_chunk_seq),
            0,
        )
        # Prefetch only mounts a tiny window around the focused chunk
        # so the DOM stays small across many cached files. User-side
        # resume expands on click via Phase 1b/2.
        win_start = max(0, focus_idx - _PREFETCH_MOUNT_RADIUS)
        win_end = min(len(chunks), focus_idx + _PREFETCH_MOUNT_RADIUS + 1)
        _perf.mark(
            "prefetch_loop_start",
            parent_id=parent_id,
            focus_idx=focus_idx,
            win=(win_start, win_end),
            total_chunks=len(chunks),
        )
        self._diag_log(
            f"prefetch_loop_start parent={parent_id[:8]} focus={focus_idx} "
            f"win=({win_start},{win_end}) total_chunks={len(chunks)}"
        )
        n_mounted = 0
        try:
            for i in range(win_start, win_end):
                if query_sig != self._current_query_signature():
                    return
                if i in container.mounted_indices:
                    continue
                # Bail out the moment user-side mount lights up: prefetch is
                # background warming, foreground always wins.
                if self._user_mount_in_flight():
                    return
                try:
                    with _perf.span("prefetch_mount_one", idx=i):
                        self._mount_chunk_into(container, chunks[i], i, chunks)
                    n_mounted += 1
                except Exception:
                    continue
                # Wait for the chunk widget's async build so
                # ``first_match_block`` resolves before a user-side
                # click adopts this pre-mount; without this, the click
                # path's retry chain polls ~500 ms for a still-running build.
                seq = chunks[i].chunk_seq
                md_widget = container.chunk_widgets.get(seq)
                if md_widget is not None and isinstance(md_widget, FNDMarkdown):
                    with contextlib.suppress(Exception), _perf.span("prefetch_await_build", idx=i):
                        async with md_widget.lock:
                            pass
                await asyncio.sleep(0.002)
        finally:
            _perf.mark(
                "prefetch_loop_end",
                parent_id=parent_id,
                n_mounted=n_mounted,
                mounted_indices_size=len(container.mounted_indices),
                is_complete=container.is_complete,
            )
            self._diag_log(
                f"prefetch_loop_end parent={parent_id[:8]} n_mounted={n_mounted} "
                f"mounted_size={len(container.mounted_indices)} "
                f"is_complete={container.is_complete}"
            )
            if container.mounted_indices:
                evicted = self._preview_cache.put(container, protect=self._active_preview)
                for old in evicted:
                    with contextlib.suppress(Exception):
                        old.remove()
            else:
                # Loop bailed on user-mount-in-flight (or every mount raised)
                # before any chunk landed. Caching the empty container would
                # block the next prefetch attempt for this (parent_id, sig)
                # via the already-cached short-circuit; instead, drop it so a
                # later trigger (cursor move, second query) can retry cleanly.
                with contextlib.suppress(Exception):
                    container.remove()

    def _user_mount_in_flight(self) -> bool:
        task = self._preview_mount_task
        if task is None:
            return False
        try:
            return not task.done()  # type: ignore[attr-defined]
        except Exception:
            return False

    async def _drain_prefetch_sinks(self) -> None:
        """Single-consumer drainer. Runs prefetch widget-mount jobs one at a
        time and yields to user-side mount before each."""
        import asyncio
        import contextlib

        q = self._prefetch_sink_queue
        assert q is not None
        while True:
            job = await q.get()
            self._diag_log(f"drainer JOB pulled qsize={q.qsize()}")
            wait_iters = 0
            # Cooperative wait — user-side mount always preempts.
            while self._user_mount_in_flight():
                wait_iters += 1
                await asyncio.sleep(0.05)
            if wait_iters > 0:
                self._diag_log(f"drainer JOB started after {wait_iters * 50}ms wait")
            try:
                await job()
            except Exception as e:
                self._diag_log(f"drainer JOB threw: {type(e).__name__}: {e}")
            with contextlib.suppress(Exception):
                q.task_done()
            await asyncio.sleep(0)

    async def _mount_chunks_async(
        self,
        parent_id: str,
        focus_chunk_seq: int,
        chunks: list[FileChunk],
        container: PreviewContainer,
        *,
        skip_internal_scrolls: bool = False,
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

        needs_pre_reveal = container.parent is None or container.has_class("-hidden")
        if container.parent is None:
            await pane.remove_children("#placeholder")
            await pane.mount(container)
        else:
            await pane.remove_children("#placeholder")
        await self._cancel_prefetch_task_on(container)
        self._activate_preview_container(
            container, pre_reveal=needs_pre_reveal, keep_outgoing=needs_pre_reveal
        )
        cold_mount = needs_pre_reveal
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
            # Phase 1a: mount the focused chunk first and yield so it
            # paints before the surrounding context mounts. On large
            # files the rest of the visible window can take several
            # hundred ms to mount; the user clicked a specific match
            # and should see THAT chunk's content first, not stare at a
            # progress bar while neighbouring chunks slowly fill in.
            if focus_idx not in container.mounted_indices:
                self._mount_chunk_into(container, chunks[focus_idx], focus_idx, chunks)
            # Event-based finalize: parallel task awaits the focused
            # chunk widget's lock (Markdown.update build-done signal)
            # before scrolling. Replaces the polling retry chain which
            # raced layout on heavy md (cold) AND lost the scroll on
            # out-of-window same-file navigation (warm-resume) because
            # the freshly-mounted chunk's region was still 0 when the
            # 30-retry budget expired.
            if cold_mount or not skip_internal_scrolls:
                import time as _time

                # Reference held on the container so GC doesn't collect
                # the task mid-await (RUF006). Cleared once it completes.
                # The above-window chunks Phase 1b will mount. finalize must
                # wait for THESE to exist + build, not just whatever is in
                # chunk_widgets when it first looks — a prefetched focus chunk
                # has build_done already set, so finalize would otherwise run
                # before Phase 1b mounts the window and scroll to a stale
                # (focus-at-top) position, then settle-scroll once they land.
                expected_above_seqs = [chunks[i].chunk_seq for i in range(win_start, focus_idx)]
                _finalize_task = asyncio.create_task(
                    self._finalize_via_lock(
                        container,
                        focus_chunk_seq,
                        _time.perf_counter(),
                        expected_above_seqs=expected_above_seqs,
                        path="cold_via_lock" if cold_mount else "warm_via_lock",
                    )
                )
                container._finalize_task = _finalize_task  # type: ignore[attr-defined]
            self._update_progress_bar(progress=len(container.mounted_indices))
            await asyncio.sleep(0)

            # Phase 1b: mount the visible window. Closest-to-focus first.
            max_offset = max(focus_idx - win_start, win_end - 1 - focus_idx)
            for offset in range(1, max_offset + 1):
                below = focus_idx + offset
                if below < win_end and below not in container.mounted_indices:
                    self._mount_chunk_into(container, chunks[below], below, chunks)
                above = focus_idx - offset
                if win_start <= above and above not in container.mounted_indices:
                    self._mount_chunk_into(container, chunks[above], above, chunks)
            self._update_progress_bar(progress=len(container.mounted_indices))
            await asyncio.sleep(0)

            # Phase 2a: background fill BELOW the window, capped at the
            # lazy-mount radius. Kept SMALL so first paint only needs the
            # window — Option C's full-mount is deferred to Phase 3, strictly
            # after the reveal, so it never delays first paint.
            below_end = min(len(chunks), focus_idx + 1 + _BACKGROUND_FILL_RADIUS)
            for i in range(win_end, below_end):
                if i in container.mounted_indices:
                    continue
                self._mount_chunk_into(container, chunks[i], i, chunks)
                self._update_progress_bar(progress=len(container.mounted_indices))
                await asyncio.sleep(0.002)
            await asyncio.sleep(0)

            # Phase 2b: hidden-prepend ABOVE the window, capped at the
            # same radius. display=False keeps the focused chunk
            # anchored while earlier sections mount.
            above_start = max(0, focus_idx - _BACKGROUND_FILL_RADIUS)
            for i in range(win_start - 1, above_start - 1, -1):
                if i in container.mounted_indices:
                    continue
                before = set(container.children)
                self._mount_chunk_into(container, chunks[i], i, chunks)
                for w in container.children:
                    if w not in before:
                        w.display = False
                        hidden_widgets.append(w)
                self._update_progress_bar(progress=len(container.mounted_indices))
                # Wall-clock yield — see prefetch loop.
                await asyncio.sleep(0.002)

            # Reveal + anchor in one synchronous block so Textual
            # folds both layout changes into a single paint — no
            # visible "shift down then scroll back up" sequence.
            if hidden_widgets:
                for w in hidden_widgets:
                    w.display = True
                hidden_widgets.clear()
                if not skip_internal_scrolls and focus_chunk_seq in container.chunk_widgets:
                    # Revealing the above-window chunks shifted the layout, so
                    # the focus chunk must be re-anchored. Scroll to the MATCH
                    # (first_match_block), not the chunk's top edge — anchoring
                    # to the top pushes a match deep inside the chunk off-screen
                    # the moment the background fill completes (the cold-load
                    # "wrong position until expanded" symptom).
                    with contextlib.suppress(Exception):
                        self._preview_scroll.reconcile()

            # Phase 3 (Option C): the first view has now painted (finalize
            # revealed during Phase 1/2). Fill the REST of the file in the
            # background so internal match-jumps land on an already-mounted
            # chunk. Strictly AFTER the reveal so it never delays first paint;
            # outward in small batches via _lazy_mount_batch (which keeps the
            # view anchored when prepending above); generous yields so it never
            # starves interaction; budget-capped so monster files stay windowed;
            # bails the instant the user navigates away.
            if len(chunks) <= _FULLMOUNT_CHUNK_BUDGET:
                # Wait for finalize to actually reveal (first paint) before adding
                # any DOM — otherwise this fill runs on the same coroutine and
                # starves the finalize task, delaying first paint several-fold.
                _ft = getattr(container, "_finalize_task", None)
                if _ft is not None:
                    with contextlib.suppress(Exception):
                        await _ft
                await asyncio.sleep(0.05)
                batch_size = 6
                # Fill BELOW only: appending in document order grows content
                # DOWNWARD, so the match the user is reading never moves — no
                # flicker. Downward match-jumps land on a mounted chunk (instant).
                # We deliberately DON'T pre-fill ABOVE: inserting content above the
                # viewport shoves it down, and the scroll can only re-pin a frame
                # later (layout is async), so a passive above-fill always jitters
                # the viewport. Upward jumps instead rebuild on demand (~140ms,
                # correct, flicker-free) — movement during a deliberate jump is
                # expected; movement while the user sits still is not.
                # Empty-guard (degenerate mount); and bail the moment the user
                # takes scroll control (a user scroll clears is_armed) so upward
                # lazy-mount isn't walled behind this background below-fill —
                # once we stop, _preview_mount_task completes and lazy-mount
                # handles both directions on demand.
                i = (
                    (max(container.mounted_indices) + 1)
                    if container.mounted_indices
                    else len(chunks)
                )
                while (
                    i < len(chunks)
                    and self._active_preview is container
                    and self._preview_scroll.is_armed
                ):
                    if i not in container.mounted_indices:
                        with contextlib.suppress(Exception):
                            self._mount_chunk_into(container, chunks[i], i, chunks)
                    i += 1
                    if i % batch_size == 0:
                        await asyncio.sleep(0.006)
        finally:
            # Always reveal any widgets we hid; a cancelled task that
            # left them hidden would leak a half-displayed container
            # into the cache.
            for w in hidden_widgets:
                with contextlib.suppress(Exception):
                    w.display = True
            # Cache the container even when the mount didn't run to
            # completion. For monster files (1000+ page PDFs with
            # thousands of chunks) the user reliably navigates away
            # before is_complete becomes True; without caching the
            # partial container, every revisit re-mounts from scratch
            # and the file looks like it has no cache. The resume path
            # in ``_dispatch_preview_mount`` skips already-mounted
            # indices so partial-cache hits paint the previously-
            # mounted region instantly and continue the fill in the
            # background. The container we just mounted IS the active one,
            # so it's protected from its own eviction by definition (it just
            # got moved to the MRU slot).
            evicted = self._preview_cache.put(container, protect=container)
            for old in evicted:
                with contextlib.suppress(Exception):
                    old.remove()
            if container.is_complete:
                self._hide_progress_bar()
            # Re-anchor only needed for cancellation case: a successful
            # Phase 2b reveal+anchor inline already scrolled to the
            # focused chunk. The inline anchor sees the post-reveal
            # widget y and lands accurately; an additional chained
            # anchor here would compete with the inline one and can
            # land at a slightly different y if Textual processes more
            # mounts in between, producing the "jump after settle" the
            # user reports.
            self._refresh_status()

    def _schedule_preview_lazy_mount_check(self, *, user_initiated: bool = False) -> None:
        """Debounced entry point. Every scroll change re-arms a short
        timer; only the *last* scroll in a burst actually runs the
        check. Coalesces programmatic anchor scrolls (which fire one or
        two watcher trips back-to-back) AND user wheel/key bursts down
        to a single check at the tail end — no fighting between the
        navigation's own scroll-to-widget and lazy-mount's compensate."""
        import contextlib

        # A genuine user scroll (pane focused, and not one of the controller's
        # own reconcile scrolls) hands scroll control back to the user: release
        # the anchor so lazy-mount-on-scroll resumes. Programmatic scrolls from
        # navigation / container swaps trip this watcher too, but with the
        # results tree focused, so they don't release.
        if (
            user_initiated
            and self._preview_scroll.is_armed
            and not self._preview_scroll_reconciling
        ):
            self._preview_scroll.release()
        if self._lazy_mount_check_timer is not None:
            with contextlib.suppress(Exception):
                self._lazy_mount_check_timer.stop()  # type: ignore[attr-defined]
        self._lazy_mount_check_timer = self.set_timer(
            0.12, self._check_preview_lazy_mount, name="lazy-mount-debounce"
        )

    def _check_preview_lazy_mount(self) -> None:
        """Scroll watcher entry point (after debounce). Mounts the next
        batch of chunks in the scroll direction when the viewport
        approaches a boundary of the chunk currently under it. Looks
        at the next *unmounted* chunk in document order — so gaps left
        behind when the user jumps between matches get filled
        progressively, not just the chunks past the absolute max/min
        mounted index."""
        self._lazy_mount_check_timer = None
        # Suppress lazy-mount only while a navigation is still settling (the
        # controller owns the position until its scroll commits). Once the
        # reveal lands the gate opens, so user scrolls by ANY means — keyboard
        # OR an unfocused mouse-wheel — extend the window. Gating on is_armed
        # instead dead-ended wheel-scroll: the anchor stays armed across navs
        # and only release() (a focused user scroll) cleared it.
        if self._preview_scroll.is_settling:
            return
        container = self._active_preview
        if container is None:
            return
        chunks = self._chunk_cache.get(container.parent_doc_id)
        if not chunks or not container.mounted_indices:
            return
        if len(container.mounted_indices) >= len(chunks):
            return
        # Don't compete with the initial visible-first mount task; it
        # owns the window and will hand off once it settles.
        if self._user_mount_in_flight():
            return
        task = self._lazy_mount_task
        if task is not None:
            try:
                if not task.done():  # type: ignore[attr-defined]
                    return
            except Exception:
                pass
        try:
            pane = self.query_one("#preview_pane", VerticalScroll)
        except Exception:
            return
        if pane.size.height <= 0:
            return

        scroll_y = float(pane.scroll_y)
        viewport_h = float(pane.size.height)
        viewport_bottom = scroll_y + viewport_h
        margin = float(_LAZY_MOUNT_TRIGGER_MARGIN)

        # Snapshot mounted chunks' virtual-y ranges so we can find the
        # widgets covering viewport top + bottom in O(mounted) — small
        # under realistic mount counts.
        chunk_ranges: list[tuple[int, int, int]] = []
        for idx in sorted(container.mounted_indices):
            seq = chunks[idx].chunk_seq
            widget = container.chunk_widgets.get(seq)
            if widget is None:
                continue
            try:
                vr = widget.virtual_region  # type: ignore[attr-defined]
                y0 = int(vr.y)
                h = int(vr.height)
            except Exception:
                continue
            chunk_ranges.append((idx, y0, y0 + h))
        if not chunk_ranges:
            return

        def _covering(y: float) -> tuple[int, int, int] | None:
            for entry in chunk_ranges:
                if entry[1] <= y < entry[2]:
                    return entry
            return None

        import asyncio

        bottom_cover = _covering(viewport_bottom - 1) or chunk_ranges[-1]
        bottom_idx, _, bottom_chunk_y1 = bottom_cover
        # Only fire below if the IMMEDIATE next chunk in document order
        # is unmounted — otherwise the user is mid-region and can
        # scroll on through the contiguous mounted span without a wall.
        next_idx = bottom_idx + 1
        if (
            next_idx < len(chunks)
            and next_idx not in container.mounted_indices
            and (bottom_chunk_y1 - viewport_bottom) <= margin
        ):
            self._lazy_mount_task = asyncio.create_task(
                self._lazy_mount_batch(container, chunks, start_idx=next_idx, direction="below")
            )
            return

        top_cover = _covering(scroll_y) or chunk_ranges[0]
        top_idx, top_chunk_y0, _ = top_cover
        prev_idx = top_idx - 1
        if (
            prev_idx >= 0
            and prev_idx not in container.mounted_indices
            and (scroll_y - top_chunk_y0) <= margin
        ):
            self._lazy_mount_task = asyncio.create_task(
                self._lazy_mount_batch(container, chunks, start_idx=prev_idx, direction="above")
            )

    async def _lazy_mount_batch(
        self,
        container: PreviewContainer,
        chunks: list[FileChunk],
        *,
        start_idx: int,
        direction: str,
    ) -> None:
        """Mount ``_LAZY_MOUNT_BATCH`` chunks starting at ``start_idx``,
        moving in ``direction``.

        Below: append in document order; no scroll adjustment needed
        because content grows downward.

        Above: hidden-prepend, await build, reveal. No scroll
        compensate — anchor preservation via virtual_region delta
        proved unreliable post-reveal (returns 0 on consecutive
        above-batches even after refresh), so newly-prepended chunks
        appear at the top of the visible area, which IS the right UX
        when the user just scrolled up to the wall.

        ``start_idx`` is the first index to mount in each direction;
        the loop skips already-mounted indices in case the gap was
        partially filled by an earlier batch.
        """
        import asyncio
        import contextlib

        if direction == "below":
            end = min(start_idx + _LAZY_MOUNT_BATCH, len(chunks))
            for i in range(start_idx, end):
                if self._active_preview is not container:
                    return
                if i in container.mounted_indices:
                    continue
                try:
                    self._mount_chunk_into(container, chunks[i], i, chunks)
                except Exception:
                    continue
                seq = chunks[i].chunk_seq
                md_widget = container.chunk_widgets.get(seq)
                if isinstance(md_widget, FNDMarkdown):
                    with contextlib.suppress(Exception):
                        async with md_widget.lock:
                            pass
                await asyncio.sleep(0)
            return

        # Mount chunks [start_idx, start_idx-1, …, start_idx-batch+1] in reverse
        # so each new widget lands BEFORE the anchor in document order, build
        # them hidden, then reveal AND scroll-compensate so the user's view stays
        # put. The anchor is the first already-mounted chunk just below the
        # prepend region; revealing the chunks above it shifts it DOWN by their
        # combined height, so we scroll the pane by that delta — turning the old
        # "wall, jump, scroll-down-to-retrigger" into a continuous upward scroll.
        # Measuring the delta reliably needs a SETTLED layout: the earlier code
        # read the delta as 0 pre-settle and gave up on compensation, leaving the
        # wall. ``_await_preview_settled`` (Textual's message-drain) makes it
        # reliable. ``hidden`` MUST be revealed even on cancel, else display=False
        # widgets cache as blank rows ("section only shows the heading").
        end = max(start_idx - _LAZY_MOUNT_BATCH, -1)
        hidden: list[Widget] = []
        anchor_seq = chunks[start_idx + 1].chunk_seq if start_idx + 1 < len(chunks) else None
        try:
            for i in range(start_idx, end, -1):
                if self._active_preview is not container:
                    return
                if i in container.mounted_indices:
                    continue
                before_children = set(container.children)
                try:
                    self._mount_chunk_into(container, chunks[i], i, chunks)
                except Exception:
                    continue
                for w in container.children:
                    if w not in before_children:
                        w.display = False
                        hidden.append(w)
                await asyncio.sleep(0.002)

            for w in hidden:
                if isinstance(w, FNDMarkdown):
                    with contextlib.suppress(Exception):
                        async with w.lock:
                            pass

            # Capture the anchor's content position + pane scroll just before the
            # reveal grows the content above it.
            pane = self.query_one("#preview_pane", VerticalScroll)
            anchor_w = container.chunk_widgets.get(anchor_seq) if anchor_seq is not None else None
            before_y = anchor_w.virtual_region.y if anchor_w is not None else None
            before_scroll = pane.scroll_y

            for w in hidden:
                w.display = True
            hidden.clear()

            # Re-anchor: scroll by however far the anchor moved down, so the
            # prepended chunks extend the scrollable region UPWARD without moving
            # the user's view — continuous scroll instead of a wall.
            if anchor_w is not None and before_y is not None:
                await self._await_preview_settled()
                if self._active_preview is container:
                    delta = anchor_w.virtual_region.y - before_y
                    if delta > 0:
                        self.begin_reconcile_scroll()
                        try:
                            pane.scroll_to(y=before_scroll + delta, animate=False, immediate=True)
                        finally:
                            self.end_reconcile_scroll()
        finally:
            # Cancellation or unexpected return: anything still in
            # ``hidden`` would otherwise stay invisible on the cached
            # container.
            for w in hidden:
                with contextlib.suppress(Exception):
                    w.display = True

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
        before_widget: Widget | None = None
        next_mounted = min(
            (j for j in container.mounted_indices if j > index),
            default=-1,
        )
        if next_mounted >= 0:
            before_seq = all_chunks[next_mounted].chunk_seq
            before_widget = container.chunk_widgets.get(before_seq)

        # Structural renderer (markdown widget) for formats whose
        # extractor populated body_md; per-line plain layout for
        # everything else (PDF, TXT). Save current widgets-by-chunk_seq
        # so the mount helpers fill the per-container dicts.
        if _uses_markdown_renderer(chunk):
            self._mount_structured_chunk(container, chunk, before=before_widget)
        else:
            self._mount_plain_chunk(container, chunk, before=before_widget)
        container.mounted_indices.add(index)

    @property
    def _scrollbar_markers_enabled(self) -> bool:
        """In-development scrollbar match highlighting — off unless the
        user opts in via ``[defaults] scrollbar_match_highlight``."""
        return bool(self._config and self._config.defaults.scrollbar_match_highlight)

    def _refresh_match_scrollbar(self, chunks: list[FileChunk]) -> None:
        """Forward line-weighted match positions to the preview's custom
        scrollbar so markers sit where the matches actually render.

        Earlier this fed a bool-per-chunk map placed by chunk ordinal,
        which ignored chunk size — a match in a short chunk after a long
        one landed near the top instead of far down. ``structural_match_
        lines`` weights by each chunk's line count instead. On large
        markdown the lazy-mounted track spans only part of the file, so
        this stays behind the in-development toggle."""
        try:
            pane = self.query_one("#preview_pane", MatchAwareScroll)
        except Exception:
            return
        if not self._scrollbar_markers_enabled:
            # Clear any markers a prior (enabled) load left, so toggling
            # the feature off takes effect on the next preview load.
            pane.set_match_lines([], 0)
            return
        from fnd.tui.preview_markers import structural_match_lines

        match_lines, total_lines = structural_match_lines(chunks, self._effective_match_spec)
        pane.set_match_lines(match_lines, total_lines)

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
        for i, c in enumerate(chunks):
            if _uses_markdown_renderer(c):
                self._mount_structured_chunk(container, c)
            else:
                self._mount_plain_chunk(container, c)
            container.mounted_indices.add(i)

    def _mount_plain_chunk(
        self,
        parent: Container | VerticalScroll,
        c: FileChunk,
        *,
        before: Widget | None = None,
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
        _, pieces = render_chunk_pieces(
            c, query=self._current_query, match_spec=self._effective_match_spec
        )
        first_widget: Static | None = None
        first_match: Static | None = None
        for line_text, has_match in pieces:
            line_w = Static(line_text, classes="chunk-line")
            line_w.fnd_text = line_text  # type: ignore[attr-defined]
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
        # Write to the owning container only. The app-level alias dicts get
        # refreshed by _activate_preview_container so writing here would
        # corrupt whichever container is currently active (esp. during
        # concurrent prefetch on a different file).
        if isinstance(parent, PreviewContainer):
            parent.chunk_widgets[c.chunk_seq] = first_widget
            parent.match_targets[c.chunk_seq] = first_match or first_widget
        else:
            self._chunk_widgets[c.chunk_seq] = first_widget
            self._match_targets[c.chunk_seq] = first_match or first_widget

    def _mount_structured_chunk(
        self,
        parent: Container | VerticalScroll,
        c: FileChunk,
        *,
        before: Widget | None = None,
    ) -> None:
        """Structural markdown rendering for formats whose extractor
        populated ``body_md`` (md / docx / pptx).

        Mounts a single :class:`FNDMarkdown` widget per chunk —
        Textual builds out the per-block widget tree (headings,
        paragraphs, tables, fenced code, lists, blockquotes) and our
        highlight-aware subclasses overlay match-only spans on the
        rendered Content. Code fences (``FNDMarkdownFence``) keep the
        Rich syntax highlighting and add the match overlay on top, so
        query terms inside a code block are highlighted too.

        ``_chunk_widgets`` maps the chunk seq to the FNDMarkdown
        widget itself (used for chunk-boundary scrolling); ``_match_
        targets`` maps to ``first_match_block`` when the chunk has
        matches, falling back to the FNDMarkdown so scroll still
        lands at the chunk top when nothing matched.
        """
        source = c.body_md or _legacy_blocks_to_md(c.blocks)
        import os

        if os.environ.get("_FND_W_HYBRID") == "1":
            from fnd.tui._md_hybrid import FNDChunkHybrid

            try:
                pane_widget = self.query_one("#preview_pane", VerticalScroll)
                wrap_width = max(20, pane_widget.content_size.width - 1)
            except Exception:
                wrap_width = 80
            md_widget = FNDChunkHybrid(
                source,
                match_spec=self._effective_match_spec,
                wrap_width=wrap_width,
                classes="chunk-section chunk-md-body chunk-first",
            )
        else:
            md_widget = FNDMarkdown(
                source,
                match_spec=self._effective_match_spec,
                # Default-on: honour the model default when no config is injected.
                render_mermaid=(self._config.defaults.render_mermaid if self._config else True),
                classes="chunk-section chunk-md-body chunk-first",
            )
        parent.mount(md_widget, before=before)
        # See _mount_plain_chunk for why we write only to the owning
        # container (concurrent prefetch on a different file would
        # otherwise overwrite the active container's dict).
        if isinstance(parent, PreviewContainer):
            parent.chunk_widgets[c.chunk_seq] = md_widget
            parent.match_targets[c.chunk_seq] = md_widget
        else:
            self._chunk_widgets[c.chunk_seq] = md_widget
            self._match_targets[c.chunk_seq] = md_widget

    def _diag_log(self, msg: str) -> None:
        # _FND_PREVIEW_DIAG=1 appends to /tmp/fnd-preview-diag.log.
        # Investigation-only; remove once findings recorded.
        import os
        import time as _time

        if not os.environ.get("_FND_PREVIEW_DIAG"):
            return
        try:
            # Hardcoded /tmp path is intentional: opt-in dev
            # instrumentation gated by FND_PREVIEW_DIAG=1, slated for
            # removal once the preview-perf investigation closes (see
            # docs/PREVIEW_DOM_PLAN.md). Production code paths do not
            # touch /tmp.
            with open("/tmp/fnd-preview-diag.log", "a") as f:  # noqa: S108
                f.write(f"[{_time.monotonic():.3f}] {msg}\n")
        except Exception:
            pass

    def action_diag_dump_preview(self) -> None:
        # Walks the active preview and writes a per-type widget count
        # to /tmp/fnd-preview-diag.log. Always on (ignores the
        # env-var gate the log writes use) so a one-key tap works.
        from collections import Counter

        lines: list[str] = ["--- dump_preview ---"]
        active = self._active_preview
        flat = self._active_flat_buffer
        if active is None and flat is None:
            lines.append("no active preview")
        if active is not None:
            lines.append(
                f"structural parent_id={active.parent_doc_id} chunks={len(active.chunk_widgets)}"
            )
            total = Counter()
            for seq, header in active.chunk_widgets.items():
                per_chunk = Counter()
                per_chunk[type(header).__name__] += 1
                for w in header.query("*"):
                    per_chunk[type(w).__name__] += 1
                total.update(per_chunk)
                top = ", ".join(f"{k}={v}" for k, v in per_chunk.most_common(5))
                lines.append(f"  chunk seq={seq} total={sum(per_chunk.values())} top5: {top}")
            lines.append(f"structural totals: {dict(total.most_common())}")
        if flat is not None:
            lines.append(
                f"flat parent_id={getattr(flat, 'parent_doc_id', '?')} "
                f"lines={len(getattr(flat, 'lines', []) or [])}"
            )
        try:
            # See _diag_log above for the /tmp rationale.
            with open("/tmp/fnd-preview-diag.log", "a") as f:  # noqa: S108
                f.write("\n".join(lines) + "\n")
        except Exception:
            pass
        self.notify(
            "Dumped preview widget tree → /tmp/fnd-preview-diag.log",
            timeout=2,
        )

    # ── StructuralHost accessors ──────────────────────────────────
    # The structural scroll strategy reads the pane, chunk/match maps,
    # match spec and lazy-mount gate back off the app through these.
    def preview_pane(self) -> VerticalScroll:
        return self.query_one("#preview_pane", VerticalScroll)

    def effective_match_spec(self) -> MatchSpec:
        return self._effective_match_spec

    def diag_log(self, msg: str) -> None:
        self._diag_log(msg)

    @property
    def chunk_widgets(self) -> dict[int, Widget]:
        return self._chunk_widgets

    @property
    def match_targets(self) -> dict[int, Widget]:
        return self._match_targets

    def active_flat_buffer(self) -> LineBufferPreview | None:
        return self._active_flat_buffer

    def begin_reconcile_scroll(self) -> None:
        self._preview_scroll_reconciling = True

    def end_reconcile_scroll(self) -> None:
        self._preview_scroll_reconciling = False

    def _select_scroll_strategy(self) -> ScrollStrategy | None:
        """Pick the active preview's scroll strategy: the flat line-buffer when
        one is showing (PDF/TXT), else the structural per-chunk strategy."""
        if self._active_flat_buffer is not None:
            return self._preview_scroll_flat
        return self._preview_scroll_structural

    # ── Open dispatch ─────────────────────────────────────────────

    # Note: we deliberately do NOT bind Tree.NodeSelected to opener.open_smart.
    # Per user feedback, clicking / Enter should populate the preview only;
    # opening externally requires the explicit `o` (open at locator) or `O`
    # (open default app) bindings. Selection still fires NodeHighlighted
    # which drives the preview render via `_on_tree_highlight`.

    @staticmethod
    def _target_for_node(node: TreeNode[Any]) -> tuple[FileGroup, Hit] | None:
        return ResultsView.target_for_node(node)

    # ── Async indexer plumbing ────────────────────────────────────

    def start_indexer(
        self,
        *,
        collection: str,
        config: Any = None,  # CollectionConfig; Any to avoid import cycle
        index_dir: Path | None = None,
        rebuild: bool = False,
        open_modal: bool = True,
        texturise_override: bool | None = None,
        skip_unchanged: bool = True,
        force_fresh: bool = False,
        _bump_seq: bool = True,
    ) -> bool:
        """Spawn the async indexer task for ``collection`` — see
        :meth:`IndexerService.start`. Kept as a real app method so
        chain continuations and test patches address the app."""
        return self._indexer.start(
            collection=collection,
            config=config,
            index_dir=index_dir,
            rebuild=rebuild,
            open_modal=open_modal,
            texturise_override=texturise_override,
            skip_unchanged=skip_unchanged,
            force_fresh=force_fresh,
            _bump_seq=_bump_seq,
        )

    # ── Indexer delegation (state lives on IndexerService) ────────
    # The indexer modal and settings screens read AND write these
    # names on the app; the property pairs keep that surface stable
    # while the service owns the state.

    @property
    def _indexer_task(self) -> asyncio.Task[None] | None:
        return self._indexer.task

    @_indexer_task.setter
    def _indexer_task(self, value: asyncio.Task[None] | None) -> None:
        self._indexer.task = value

    @property
    def _indexer_cancel(self) -> asyncio.Event | None:
        return self._indexer.cancel

    @_indexer_cancel.setter
    def _indexer_cancel(self, value: asyncio.Event | None) -> None:
        self._indexer.cancel = value

    @property
    def _indexer_events(self) -> asyncio.Queue[Any] | None:
        return self._indexer.events

    @_indexer_events.setter
    def _indexer_events(self, value: asyncio.Queue[Any] | None) -> None:
        self._indexer.events = value

    @property
    def _indexer_state(self) -> Any:
        return self._indexer.state

    @_indexer_state.setter
    def _indexer_state(self, value: Any) -> None:
        self._indexer.state = value

    @property
    def _indexer_last_event(self) -> Any:
        return self._indexer.last_event

    @_indexer_last_event.setter
    def _indexer_last_event(self, value: Any) -> None:
        self._indexer.last_event = value

    @property
    def _indexer_run_seq(self) -> int:
        return self._indexer.run_seq

    @_indexer_run_seq.setter
    def _indexer_run_seq(self, value: int) -> None:
        self._indexer.run_seq = value

    @property
    def _indexer_deferred_task(self) -> asyncio.Task[None] | None:
        return self._indexer.deferred_task

    @_indexer_deferred_task.setter
    def _indexer_deferred_task(self, value: asyncio.Task[None] | None) -> None:
        self._indexer.deferred_task = value

    @property
    def _indexer_chain_remaining(self) -> list[str]:
        return self._indexer.chain_remaining

    @_indexer_chain_remaining.setter
    def _indexer_chain_remaining(self, value: list[str]) -> None:
        self._indexer.chain_remaining = value

    @property
    def _indexer_chain_total(self) -> int:
        return self._indexer.chain_total

    @_indexer_chain_total.setter
    def _indexer_chain_total(self, value: int) -> None:
        self._indexer.chain_total = value

    @property
    def _indexer_chain_callback_pending(self) -> bool:
        return self._indexer.chain_callback_pending

    @_indexer_chain_callback_pending.setter
    def _indexer_chain_callback_pending(self, value: bool) -> None:
        self._indexer.chain_callback_pending = value

    @property
    def _indexer_chain_history(self) -> list[Any]:
        return self._indexer.chain_history

    @_indexer_chain_history.setter
    def _indexer_chain_history(self, value: list[Any]) -> None:
        self._indexer.chain_history = value

    @property
    def _indexer_texturise_override(self) -> bool | None:
        return self._indexer.texturise_override

    @_indexer_texturise_override.setter
    def _indexer_texturise_override(self, value: bool | None) -> None:
        self._indexer.texturise_override = value

    @property
    def _indexer_skip_unchanged(self) -> bool:
        return self._indexer.skip_unchanged

    @_indexer_skip_unchanged.setter
    def _indexer_skip_unchanged(self, value: bool) -> None:
        self._indexer.skip_unchanged = value

    @property
    def _indexer_force_fresh(self) -> bool:
        return self._indexer.force_fresh

    @_indexer_force_fresh.setter
    def _indexer_force_fresh(self, value: bool) -> None:
        self._indexer.force_fresh = value

    @property
    def _indexer_rebuild(self) -> bool:
        return self._indexer.rebuild

    @_indexer_rebuild.setter
    def _indexer_rebuild(self, value: bool) -> None:
        self._indexer.rebuild = value

    @property
    def _indexer_collection(self) -> str:
        return self._indexer.collection

    @_indexer_collection.setter
    def _indexer_collection(self, value: str) -> None:
        self._indexer.collection = value

    @property
    def _indexer_started_at(self) -> str:
        return self._indexer.started_at

    @_indexer_started_at.setter
    def _indexer_started_at(self, value: str) -> None:
        self._indexer.started_at = value

    def action_reindex_default(self) -> None:
        """Convenience action: reindex the default collection."""
        try:
            self._reindex_with_warning_if_needed("default")
        except Exception as e:
            self.notify(f"Could not start indexer: {e}", severity="error")

    def _reindex_with_warning_if_needed(
        self,
        collection: str,
        *,
        texturise_override: bool | None = None,
        skip_unchanged: bool = True,
        force_fresh: bool = False,
        rebuild: bool = False,
    ) -> None:
        self._indexer.reindex_with_warning(
            collection,
            texturise_override=texturise_override,
            skip_unchanged=skip_unchanged,
            force_fresh=force_fresh,
            rebuild=rebuild,
        )

    def _maybe_resume_indexer(self) -> None:
        self._indexer.maybe_resume()

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
        For MD / TXT, the chunk's ``line`` (plus per-source app override
        and Obsidian vault from app_params) flows through to the
        resolved handler so templates like ``code -g {path}:{line}:1``
        jump to the right line.
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
            page_label=str(getattr(hit, "page_label", "") or ""),
            slide=getattr(hit, "slide", 0),
            heading_path=getattr(hit, "heading_path", ""),
            line=getattr(hit, "line", 0),
            query=self._current_query,
            source=self._source_for_hit(hit),
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

    def action_open_with_menu(self) -> None:
        """Show the 'Open with…' modal for the focused hit.

        Lists every registered app whose ``handles`` covers the hit's
        kind; the resolved default is highlighted and Enter fires it.
        Builds the registry, resolves the default app, and finds the
        source the hit belongs to (so per-source ``app_for`` / vault
        overrides take effect).
        """
        tree = self.query_one("#results_pane", Tree)
        if tree.cursor_node is None:
            return
        target = self._target_for_node(tree.cursor_node)
        if target is None:
            return
        _, hit = target

        from fnd import apps as apps_mod
        from fnd.config import load as load_config
        from fnd.tui.open_with_screen import OpenWithScreen

        try:
            cfg = load_config()
        except Exception:
            cfg = None

        registry = apps_mod.build_registry(cfg) if cfg is not None else apps_mod.BUILTIN_APPS
        app_defaults: dict[str, str] = dict(getattr(cfg, "app_defaults", {})) if cfg else {}
        # Mirror open_smart's auto-promote ladder so the modal's
        # highlighted default matches what `o` would fire.
        if "pdf" not in app_defaults:
            if opener._has_skim():
                app_defaults["pdf"] = "skim"
            elif apps_mod.BUILTIN_APPS["preview"].available() and apps_mod.ax_trusted():
                app_defaults["pdf"] = "preview"

        source = self._source_for_hit(hit)
        resolved = apps_mod.resolve_app(
            kind=hit.kind,
            source=source,
            app_defaults=app_defaults,
            registry=registry,
        )
        # The modal needs the raw query string for the OpenRequest.
        hit_with_query = _HitWithQuery(hit, self._current_query)
        self.push_screen(
            OpenWithScreen(
                hit=hit_with_query,
                source=source,
                registry=registry,
                default_id=resolved.id,
            )
        )

    def _source_for_hit(self, hit: Any) -> Any | None:
        """Find the :class:`fnd.config.SourceConfig` whose root contains
        ``hit.path``. Used by the open-with modal to surface per-source
        app overrides + vault params. Returns ``None`` if no source
        matches (eg. legacy index without source metadata)."""
        cfg = getattr(self, "_config", None)
        if cfg is None:
            return None
        try:
            hit_path = Path(hit.path).expanduser().resolve()
        except (ValueError, OSError):
            return None
        # Scan every collection in the active scope; first containing
        # source wins. The hit's parent_id encodes its source path so a
        # future refactor could short-circuit via index lookup.
        active = self._collections or list(cfg.collections.keys())
        for name in active:
            coll = cfg.collections.get(name)
            if coll is None:
                continue
            for src in coll.sources:
                try:
                    root = Path(src.path).expanduser().resolve()
                except (ValueError, OSError):
                    continue
                try:
                    hit_path.relative_to(root)
                except ValueError:
                    continue
                return src
        return None

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
        - Collapsed branch with children → expand it AND drop the cursor
          onto its first child (the preview already shows that child, so
          leaving the cursor on the parent file row would force a wasted
          Down keypress before a fresh match comes into view).
        - Already-expanded branch → move cursor to its first child.
        - Leaf in the results tree → bridge focus to the preview pane
          (the user has already pinned the match; the next natural move
          is to start reading).
        - Leaf elsewhere / no children → no-op.
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
            if node is not None and tree.id == "results_pane":
                self.action_focus_preview_pane()
            return
        if not node.is_expanded:
            node.expand()
        self._move_cursor_to_first_child(tree, node)

    @staticmethod
    def _move_cursor_to_first_child(tree: Tree[Any], node: TreeNode[Any]) -> None:
        """Drop the cursor onto ``node``'s first child, robust against a
        stale line cache.

        ``expand()`` only invalidates the cache; until the tree rebuilds,
        the freshly-revealed children still carry a stale ``_line`` of -1.
        Going through ``move_cursor(child)`` then sets ``cursor_line`` to -1,
        which the skip-expanded-parents validator clamps to 0 and walks into
        a jump to the first file — visible when the rebuild is delayed (e.g.
        the preview pane is mid full-mount). ``move_cursor_to_line`` forces
        the rebuild (it reads ``_tree_lines``) and the first child always
        sits one row below its parent, so the move is correct synchronously.
        """
        if not node.children:
            return
        tree.move_cursor_to_line(node.line + 1)

    @property
    def _effective_match_spec(self) -> MatchSpec:
        """The MatchSpec the renderers should consult. Falls back to an
        empty spec when the user has toggled highlights off — the
        preview pane then renders the plain document with no yellow /
        orange overlays and no scrollbar match markers."""
        return self._current_match_spec if self._highlights_enabled else MatchSpec()

    def action_toggle_highlights(self) -> None:
        self._search.toggle_highlights()

    def action_toggle_fuzzy(self) -> None:
        self._search.toggle_fuzzy()

    def _rerender_current_preview(self) -> None:
        """Drop the preview cache (its widgets carry already-applied
        highlight spans, so they can't simply re-paint themselves) and
        re-issue the render for the focused result. Used by
        ``action_toggle_highlights`` so the new overlay state lands
        without waiting for the user to move the cursor."""
        import contextlib

        # Re-use the per-query cache invalidation: clear decoded
        # chunks, kill any in-flight mount worker, drop cached
        # PreviewContainers from the DOM, reset alias maps.
        self._chunk_cache.clear()
        self._prebuilt_cache.clear()
        self._cancel_preview_mount_task()
        self._cancel_lazy_mount_task()
        evicted = self._preview_cache.clear()
        for old in evicted:
            with contextlib.suppress(Exception):
                old.remove()
        if self._active_preview is not None and self._active_preview.parent is not None:
            with contextlib.suppress(Exception):
                self._active_preview.remove()
        self._active_preview = None
        self._flat_buffer_cache.clear()
        self._reset_shared_flat_buffer()
        self._chunk_widgets = {}
        self._match_targets = {}
        self._preview_parent_id = None
        self._hide_progress_bar()
        # Re-trigger the preview render for the focused result. We pull
        # the (parent_id, focus_chunk_seq) pair off the cursor's
        # data — same logic ``_on_tree_highlight`` uses on cursor
        # change.
        try:
            tree = self.query_one("#results_pane", Tree)
        except Exception:
            return
        cursor = tree.cursor_node
        if cursor is None or not isinstance(cursor.data, dict):
            return
        kind = cursor.data.get("kind")
        if kind == "section":
            hit: Hit = cursor.data["hit"]
            self._render_full_doc(hit.parent_id, focus_chunk_seq=hit.chunk_seq)
        elif kind == "file":
            g: FileGroup = cursor.data["group"]
            top = g.hits[0] if g.hits else None
            self._render_full_doc(g.parent_id, focus_chunk_seq=top.chunk_seq if top else 0)

    def action_focus_results_pane(self) -> None:
        """Single-key teleport from anywhere → results tree."""
        self.query_one("#results_pane", Tree).focus()

    def action_focus_preview_pane(self) -> None:
        """Single-key teleport from anywhere → preview pane."""
        self.query_one("#preview_pane").focus()

    def action_focus_filters_panel(self) -> None:
        """Single-key teleport from anywhere → filters sidebar panel."""
        self.query_one("#filters_panel_tree", Tree).focus()

    def action_focus_collections_panel(self) -> None:
        """Single-key teleport from anywhere → collections sidebar panel."""
        self.query_one("#collections_panel_tree", Tree).focus()

    def _clear_query_results(self) -> None:
        self._search.clear_results()

    # ── Scope delegation (state lives on ScopeController) ─────────
    # Tests and sibling modules read AND write these names on the app;
    # the property pairs keep that surface stable while the controller
    # owns the state.

    @property
    def _collections(self) -> list[str]:
        return self._scope.collections

    @_collections.setter
    def _collections(self, value: list[str]) -> None:
        self._scope.collections = value

    @property
    def _active_sources(self) -> list[str]:
        return self._scope.active_sources

    @_active_sources.setter
    def _active_sources(self, value: list[str]) -> None:
        self._scope.active_sources = value

    @property
    def _filter_kinds(self) -> list[str]:
        return self._scope.filter_kinds

    @_filter_kinds.setter
    def _filter_kinds(self, value: list[str]) -> None:
        self._scope.filter_kinds = value

    @property
    def _filter_date(self) -> str:
        return self._scope.filter_date

    @_filter_date.setter
    def _filter_date(self, value: str) -> None:
        self._scope.filter_date = value

    @property
    def _collapsed_panels(self) -> set[str]:
        return self._scope.collapsed_panels

    @_collapsed_panels.setter
    def _collapsed_panels(self, value: set[str]) -> None:
        self._scope.collapsed_panels = value

    @property
    def _expanded_collections(self) -> set[str]:
        return self._scope.expanded_collections

    @_expanded_collections.setter
    def _expanded_collections(self, value: set[str]) -> None:
        self._scope.expanded_collections = value

    @property
    def _expanded_filter_branches(self) -> set[str]:
        return self._scope.expanded_filter_branches

    @_expanded_filter_branches.setter
    def _expanded_filter_branches(self, value: set[str]) -> None:
        self._scope.expanded_filter_branches = value

    def _persist_state(self) -> None:
        self._scope.persist()

    def _collection_source_ids(self, name: str) -> list[str]:
        return self._scope.collection_source_ids(name)

    def _refresh_collections_panel(self) -> None:
        self._scope.refresh_collections_panel()

    def _refresh_filters_panel(self) -> None:
        self._scope.refresh_filters_panel()

    @on(Tree.NodeSelected, "#filters_panel_tree")
    def _on_filters_panel_selected(self, ev: Tree.NodeSelected[dict[str, object]]) -> None:
        self._scope.on_filters_selected(ev)

    @on(Tree.NodeSelected, "#collections_panel_tree")
    def _on_collections_panel_selected(self, ev: Tree.NodeSelected[dict[str, object]]) -> None:
        self._scope.on_collections_selected(ev)

    @on(Tree.NodeExpanded, "#collections_panel_tree")
    def _on_collection_branch_expanded(self, ev: Tree.NodeExpanded[dict[str, object]]) -> None:
        self._scope.on_collection_branch_expanded(ev)

    @on(Tree.NodeCollapsed, "#collections_panel_tree")
    def _on_collection_branch_collapsed(self, ev: Tree.NodeCollapsed[dict[str, object]]) -> None:
        self._scope.on_collection_branch_collapsed(ev)

    @on(Tree.NodeExpanded, "#filters_panel_tree")
    def _on_filter_branch_expanded(self, ev: Tree.NodeExpanded[dict[str, object]]) -> None:
        self._scope.on_filter_branch_expanded(ev)

    @on(Tree.NodeCollapsed, "#filters_panel_tree")
    def _on_filter_branch_collapsed(self, ev: Tree.NodeCollapsed[dict[str, object]]) -> None:
        self._scope.on_filter_branch_collapsed(ev)

    def action_dismiss_overlay(self) -> None:
        """Close any remaining in-app overlay (explain, multi DSL).

        Kept as a public action so palette / command-driven dismissal
        still works. The Esc key now binds to :meth:`action_escape_back`
        which delegates here first, then cascades focus toward the
        results pane.
        """
        for selector in ("#explain_overlay", "#multi_panel"):
            for w in self.query(selector):
                w.remove()

    def action_escape_back(self) -> None:
        """One-step Esc cascade toward the results pane.

        Order of precedence (first match wins):

        1. If an in-app overlay (explain trace, :multi panel) is up,
           close it. Focus is left wherever it was.
        1.5. If reading view is active, exit it (restore the sidebar).
        2. Otherwise, branch on the current focus context:

           - ``query`` / ``preview`` / ``filters`` / ``collections``
             → focus the results pane.
           - ``results`` / ``global`` → no-op (you're already at the
             primary pane, or no pane is recognised).

        The unified Settings menu and the Collections form live on the
        screen stack and override this binding with their own ``Esc``
        handler (back one level / dismiss form). This action only fires
        when the main app screen is on top.
        """
        # Step 1 — dismiss in-app overlay if present.
        dismissed = False
        for selector in ("#explain_overlay", "#multi_panel"):
            for w in self.query(selector):
                w.remove()
                dismissed = True
        if dismissed:
            return

        # Step 1.5 — in reading view, Esc returns to the normal app
        # (restore the sidebar) rather than cascading focus.
        if self._reading_mode:
            self.action_toggle_reading_mode()
            return

        # Step 2 — cascade focus toward the results pane.
        ctx = self._focus_context()
        if ctx in ("query", "preview", "filters", "collections"):
            import contextlib

            with contextlib.suppress(Exception):
                self.query_one("#results_pane", Tree).focus()

    def action_show_help(self) -> None:
        """Open (or toggle off) the Keybindings cheat sheet.

        ``?`` always pushes Keybindings ON TOP of whatever screen the
        user is currently on so ``Esc`` from Keybindings returns them
        to exactly that screen (a sub-menu, the source-edit form, the
        Open-with modal — wherever they invoked ``?``). The previous
        behaviour popped the entire settings stack first, which
        dropped users back to the main app and made ``?`` useless as a
        "what can I do here?" affordance.

        Re-pressing ``?`` while Keybindings is the front screen pops
        it (toggle), so the same key opens and closes the cheat sheet.
        Context hint is derived from the screen below Keybindings so
        the relevant section sorts right after Global.
        """
        from fnd.tui.menu import SECTION_KEYBINDINGS
        from fnd.tui.settings_screen import (
            SettingsScreen,
            open_settings_section,
        )

        # Toggle off when already on Keybindings.
        current = self.screen
        if isinstance(current, SettingsScreen) and getattr(current, "_breadcrumb", ()) == (
            "Keybindings",
        ):
            self.pop_screen()
            return

        context_hint = self._keybindings_context_hint()
        open_settings_section(self, SECTION_KEYBINDINGS, context_hint=context_hint)

    def _keybindings_context_hint(self) -> str | None:
        """Map the current screen / focused panel to the Keybindings
        section that should appear right after Global. Returns ``None``
        when nothing more specific than Global is appropriate."""
        from fnd.tui.settings_screen import SettingsScreen, SourceFormScreen

        # If we're inside the Settings stack, the relevant section
        # depends on which screen the user is on. SourceFormScreen
        # has its own static section; SettingsScreen catches the rest
        # (Preferences / Collections / Keybindings sub-screens — the
        # SettingsList widget bindings apply across all of them).
        current = self.screen
        if isinstance(current, SourceFormScreen):
            return "Source form"
        if isinstance(current, SettingsScreen):
            return "Settings menu"

        # Main app — pick by the focused pane.
        focused = self.focused
        widget = focused
        while widget is not None:
            wid = getattr(widget, "id", "") or ""
            if wid == "preview_pane":
                return "Preview pane"
            if wid == "results_pane":
                return "Results pane"
            if wid in ("filters_panel", "filters_panel_tree"):
                return "Filters panel"
            if wid in ("collections_panel", "collections_panel_tree"):
                return "Collections panel"
            if wid == "query_bar":
                return "Query input"
            widget = widget.parent
        return None

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
        """Open the unified Settings & Commands menu.

        Replaces the original blind-typed palette: the user lands on a
        full-screen list of every action and setting, with a search Input
        at the top for free-text filtering across all sections.
        """
        from fnd.tui.settings_screen import SettingsScreen, open_settings

        if isinstance(self.screen, SettingsScreen):
            self._close_settings_stack()
            return
        open_settings(self)

    def _close_settings_stack(self) -> None:
        """Pop every nested SettingsScreen so the user returns to the
        main app. Used by the Esc cascade and by re-pressing ``:`` while
        the menu is open."""
        from fnd.tui.settings_screen import SettingsScreen

        while isinstance(self.screen, SettingsScreen):
            self.pop_screen()

    def action_open_multi_input(self) -> None:
        """Open the :multi DSL panel for typed sub-queries + intent line.

        Submit (Ctrl+J) parses via :func:`fnd.fusion.parse_multi_input`,
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

        from fnd.fusion import parse_multi_input

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
        """Palette entry: push the Collections sub-screen directly. One
        Esc returns to the main app."""
        from fnd.tui.menu import SECTION_COLLECTIONS
        from fnd.tui.settings_screen import SettingsScreen, open_settings_section

        if self._config is None:
            return
        if isinstance(self.screen, SettingsScreen):
            self._close_settings_stack()
        open_settings_section(self, SECTION_COLLECTIONS)

    def action_open_config_file(self) -> None:
        """Drop into ``$EDITOR`` on the user's config.toml; reload Config
        on return. On validation failure, push the recovery screen."""
        import os
        import subprocess

        from fnd.config import (
            CONFIG_TEMPLATE,
            default_config_path,
            load,
        )

        path = default_config_path()
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        # Close any settings screens so the editor takes over the terminal
        # cleanly; otherwise Textual's screen_stack restoration can flash a
        # half-painted menu over the freshly-loaded TUI.
        from fnd.tui.settings_screen import SettingsScreen

        while isinstance(self.screen, SettingsScreen):
            self.pop_screen()
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
        with self.suspend():
            subprocess.call([editor, str(path)])
        try:
            self._config = load()
        except Exception as e:
            from fnd.tui.config_recovery_screen import (
                ConfigRecoveryScreen,
                _format_error,
            )

            self.push_screen(
                ConfigRecoveryScreen(
                    error_text=_format_error(e, path),
                    config_path=path,
                ),
                callback=self._on_recovery_done,
            )
            return
        self._ranking_profile = self._resolve_profile()
        self._refresh_status()
        self._refresh_collections_panel()
        self.notify("Reloaded config", timeout=2)

    def action_open_keybindings_file(self) -> None:
        """Drop into $EDITOR on keybindings.toml; reload keymap on return."""
        import os
        import subprocess

        from fnd.config import default_config_path

        path = default_config_path().parent / "keybindings.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(
                '# FND user keybinding overrides.\n# [normal]\n# "j"    = "focus_results_pane"\n',
                encoding="utf-8",
            )
        # Close any settings screens so the editor takes over the terminal
        # cleanly; otherwise Textual's screen_stack restoration can flash a
        # half-painted menu over the freshly-loaded TUI.
        from fnd.tui.settings_screen import SettingsScreen

        while isinstance(self.screen, SettingsScreen):
            self.pop_screen()
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
        with self.suspend():
            subprocess.call([editor, str(path)])
        # Reload the keymap so new bindings take effect immediately.
        from fnd.tui.actions import load_keymap

        self._fnd_keymap = load_keymap()
        self.notify("Reloaded keybindings", timeout=2)

    def _on_recovery_done(self, result: object) -> None:
        from fnd.config import load

        if result == "valid":
            try:
                self._config = load()
                self._ranking_profile = self._resolve_profile()
                self._refresh_status()
                self._refresh_collections_panel()
            except Exception:
                pass

    def _reindex_collection_async(self, name: str) -> None:
        self._indexer.reindex_collection_async(name)

    def _on_reindex_complete(self) -> None:
        self._indexer.on_reindex_complete()

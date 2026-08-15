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
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from rich.text import Text


from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.scrollbar import ScrollBar
from textual.widget import Widget
from textual.widgets import (
    Input,
    Static,
    Tree,
)
from textual.widgets.tree import TreeNode

from fnd import opener, os_labels
from fnd.config import Config, default_index_dir
from fnd.launch_command import LaunchCommandSerializer, LaunchScope
from fnd.matching import MatchSpec
from fnd.query import Searcher
from fnd.tui.actions import REGISTRY, Keymap, load_keymap
from fnd.tui.indexer_service import IndexerService
from fnd.tui.match_evidence import evidence_spec_for_pass, has_paintable_match
from fnd.tui.match_navigator import MatchNavigator
from fnd.tui.preview.flat_view import FlatBufferView
from fnd.tui.preview.lazy_mount import LazyMounter
from fnd.tui.preview.prefetch import PrefetchEngine
from fnd.tui.preview.presenter import PreviewPresenter, target_from_node_data
from fnd.tui.preview_dispatcher import uses_markdown_renderer
from fnd.tui.preview_scroll import (
    FlatScrollStrategy,
    PreviewScrollController,
    ScrollStrategy,
    StructuralScrollStrategy,
)
from fnd.tui.preview_scrollbar import MatchAwareScroll, ThinScrollBarRender
from fnd.tui.progress import FNDProgressBar, ProgressFacility, ProgressSession
from fnd.tui.progress.operations import IndexProgressTracker, PreviewProgressTracker
from fnd.tui.results_labels import (
    _elide_middle_keep_suffix,
)
from fnd.tui.results_view import ResultsView
from fnd.tui.scope_panel import ScopeController
from fnd.tui.search_controller import SearchController
from fnd.tui.sidebar_layout import Panel, allocate
from fnd.tui.widgets.clear_bar import ClearFiltersBar
from fnd.tui.widgets.preview_container import (
    _HitWithQuery,
)
from fnd.tui.widgets.results_tree import ResultsTree

# App-wide thin scrollbars: every stock Textual ScrollBar (results/sidebar
# trees, code fences, settings lists) renders the thumb as a hairline glyph
# hugging the frame instead of a reverse-video full-cell block. The preview's
# MatchAwareScrollBar applies the same thinning via its own renderer subclass.
ScrollBar.renderer = ThinScrollBarRender


# Per-chunk renderer choice lives in ``preview_dispatcher`` so the
# file-level ``choose_preview_mode`` decision and the per-chunk mount
# loop can't drift apart on which kinds are markdown-rendered.
_uses_markdown_renderer = uses_markdown_renderer


def _short_label(action_id: str) -> str:
    """Footer-hint label for an action — uses Action.footer_label when set,
    otherwise falls back to the first word of the description. Localised
    because this also becomes the Textual ``Binding`` description, which no
    longer passes through the hint-bar renderer."""
    for a in REGISTRY:
        if a.id == action_id:
            if a.footer_label:
                return os_labels.localise(a.footer_label)
            return os_labels.localise(a.description).split(".")[0].split(" ")[0].rstrip(",")
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
            # Both clusters run through localise so a hint table can hold
            # ``{alt_key}`` and render ⌥ on macOS, Alt elsewhere — the footer
            # and the Keybindings page then can't disagree.
            key = os_labels.localise(key)
            label = os_labels.localise(label)
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
    /* Class-toggled focus border (not :focus-within) so it survives a
       terminal blur — Textual clears :focus-within when the app loses focus,
       which dropped the border on tab-away and reinstated it a beat late on
       return. ``on_descendant_focus`` drives the class; nothing clears it on
       blur. Mirrors the preview pane below. */
    #results_pane.-focused { border: round $accent; }
    /* height is driven at runtime by the dynamic allocator (_reflow_sidebar /
       sidebar_layout.py); the `auto` here is only the pre-reflow default. No
       max-height cap — the allocator, not CSS, decides each panel's share. */
    #collections_panel_tree {
        width: 100%; height: auto;
        border: round $primary 50%;
        overflow-x: hidden;
    }
    #collections_panel_tree.-focused { border: round $accent; }
    #filters_pane {
        width: 100%; height: auto;
        border: round $primary 50%;
        overflow-x: hidden;
    }
    #filters_pane.-focused { border: round $accent; }
    /* 1fr (not auto) so the tree fills the container and SCROLLS internally;
       auto grows to full content height and is merely clipped, stranding the
       cursor off-screen. */
    #filters_panel_tree { width: 100%; height: 1fr; border: none; overflow-x: hidden; }
    /* Docked at the top so it floats above the scrolling tree, always in view
       while a filter is active; hidden otherwise. */
    #clear_filters_bar {
        /* visibility (not display) so the row is always reserved — the bar
           appearing on the first active filter must not shove the tree down. */
        dock: top; height: 1; padding: 0 1; visibility: hidden;
        color: $primary 50%;
    }
    #clear_filters_bar:hover { color: $accent; text-style: bold; }
    #clear_filters_bar:focus { color: $accent; text-style: bold; background: $accent 15%; }
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
    #filters_pane.collapsed {
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
        launch_filters: LaunchScope | None = None,
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
        self._scope = ScopeController(self, collection=collection, launch_filters=launch_filters)
        # Reading mode: hide the sidebar so the preview fills the width for
        # clean text selection / distraction-free reading. Session-only;
        # owns mouse capture (off while reading so the terminal handles
        # drag-select, right-click Copy, ⌘C, macOS Speak-selection).
        self._reading_mode: bool = False
        # Dynamic sidebar height allocation: last-applied heights (or the
        # sentinel "collapsed") per panel, so a reflow only re-styles panels
        # whose share actually changed. Guards against relayout thrash.
        self._sidebar_height_cache: dict[str, object] = {}
        self._reflow_pending: bool = False
        self._search.ranking_profile = self._search.resolve_profile()
        # Structural preview core (caches, active/outgoing containers,
        # debounced load + mount/reveal/settle pipeline); see
        # fnd/tui/preview/presenter.py.
        self._preview = PreviewPresenter(self)
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
        # Owns the structural preview scroll-to-match logic, reading the
        # chunk/match maps and pane back off this app via the host accessors.
        self._preview_scroll_structural = StructuralScrollStrategy(host=self._preview)
        self._preview_scroll_flat = FlatScrollStrategy(host=self._preview)
        # Single source of truth for where the preview should sit: navigation
        # arms an anchor; mount/finalize events reconcile against it (idempotent
        # → the formerly racing scroll sites collapse to one target).
        self._preview_scroll = PreviewScrollController(select_strategy=self._select_scroll_strategy)
        # Scroll-driven lazy mounting (task + debounce timer); see
        # fnd/tui/preview/lazy_mount.py.
        self._lazy = LazyMounter(self)
        self._progress = ProgressFacility(self)
        # Watches the preview pipeline and drives the progress line from it, so
        # the mount path never has to report its own progress. See
        # fnd/tui/progress/operations.py.
        self._nav_progress = PreviewProgressTracker(self)
        # Mirrors a running index onto the same line. A background run
        # (auto-resume on launch) otherwise surfaces nothing but a toast.
        self._index_progress = IndexProgressTracker(self)
        # Prefetch warming pipeline (sink queue + drainer task started in
        # on_mount); see fnd/tui/preview/prefetch.py.
        self._prefetch = PrefetchEngine(self)
        # Intra-file match navigation (n/b); see fnd/tui/match_navigator.py.
        self._match_nav = MatchNavigator(self)

    def open_progress(self, phase: str = "", *, total: int = 1) -> ProgressSession:
        """Open a new ProgressSession. Use as a context manager."""
        return self._progress.open(phase, total=total)

    def on_unmount(self) -> None:
        """Stop the progress tick loop and persist what the phases actually
        cost, so the next session's pacing starts calibrated."""
        self._progress.shutdown()

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
                # The filters tree lives inside a bordered container so a clear
                # affordance can dock at the top and stay in view whatever the
                # tag list's scroll. The container wears the border / title /
                # collapse state the bare tree used to.
                with Vertical(id="filters_pane"):
                    yield ClearFiltersBar("", id="clear_filters_bar")
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
            # Reading View is pure scroll-nav — there are no result-driven
            # match-jumps here, so the full-document mount (kept for instant
            # in-file jumps in normal preview) buys nothing and just makes the
            # widen-reflow and every subsequent scroll repaint a heavier tree.
            # Drop to the visible window; scroll-driven lazy-mount refills as
            # the reader scrolls. prune_active_to_window is scroll-compensated
            # and flash-free, so the on-screen content stays put. On exit we
            # leave it windowed — the next match-nav's render_full_doc restores
            # the full mount when the results-driven workflow actually needs it.
            self._preview.prune_active_to_window()
        else:
            self.query_one("#results_pane", ResultsTree).focus()
        if location is not None:
            self.call_after_refresh(self._preview_scroll.scroll_to_location, location)
        self._refresh_preview_match_indicator()
        self._refresh_footer_hints()

    def on_mount(self) -> None:
        # Tokyo-night theme: muted blue/teal pastel palette per user request.
        self.theme = "tokyo-night"
        self._prefetch.start()

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
                    _sentinel.write_text(texture_signature(), encoding="utf-8")

        # Route fnd.apps notices through an in-app modal for AX issues, and
        # through Textual.notify for everything else. Without this hook, the
        # Preview handler's stderr fallback gets buried under the curses
        # display and the user has no idea the page-jump failed.
        from fnd import apps as _apps_mod

        _apps_mod.set_notice_sink(self._dispatch_apps_notice)

        try:
            self._search.searcher = Searcher(index_dir=self._index_dir)
        except (FileNotFoundError, RuntimeError):
            # No index yet — the app still opens so the user can manage
            # collections, then reindex outside or from the CLI.
            self._search.searcher = None
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
        self._scope.refresh_collections_panel()
        # Filters panel — kind/date selectors that compose into the query.
        ftree = self.query_one("#filters_panel_tree", Tree)
        ftree.show_root = False
        ftree.guide_depth = 2
        # Filters parents (Modified / Created / Tags headers) are no-ops on
        # Enter — skip past them when expanded. File-type *category* rows are
        # exempt: they toggle, so they stay selectable (see ResultsTree).
        ftree._skip_expanded_parents = True  # type: ignore[attr-defined]
        # Enter must TOGGLE only, never auto-expand the category (left/right do
        # expand/collapse via action_tree_smart_expand/collapse). Matches ctree.
        ftree.auto_expand = False
        self._scope.refresh_filters_panel()
        # Restore persisted panel collapse-to-header.

        for panel_id in self._scope.collapsed_panels:
            with contextlib.suppress(Exception):
                self.query_one(f"#{panel_id}").add_class("collapsed")
        self._refresh_status()
        # Focus the query bar first and let the search take it back: results
        # no longer exist synchronously after ``run()``, so there is nothing to
        # branch on here. ``_refresh_results_tree`` focuses the tree itself once
        # groups land, and skips that when there are none — the same end state
        # the old ``not self._search.groups`` check produced.
        self.query_one("#query_bar", Input).focus()
        if self._initial_query:
            self._search.run(self._initial_query)
        # Auto-resume any interrupted reindex from a previous fnd session.
        # Runs in background (no modal); user can click the footer
        # indicator or invoke `action_reindex_default` to view progress.
        # Wrapped in try/except so a corrupt state file doesn't keep the
        # TUI from launching.
        with contextlib.suppress(Exception):
            self._indexer.maybe_resume()
        # Pre-upgrade cache entries (PDFs textured on an older extractor
        # version) are surfaced passively in Settings → Indexing & PDF
        # Texture, not via a startup popup — re-texturising is a
        # preview-quality refresh the user opts into, never urgent.

    # ── Ranking profile (§7) ──────────────────────────────────────

    def _preview_title(self, edge_width: int = 0) -> str:
        """Border title for the preview pane — ``Preview — <file>``.

        ``edge_width`` is the pane's outer border-box width; when given, the
        filename is middle-elided so its extension survives instead of being
        clipped off the right edge. A round border reserves 6 cells of the
        edge (2 corners + 2 pads + 2 filler dashes — measured), so the full
        title string must fit in ``edge_width - 6``.
        """
        if self._preview.parent_id is None:
            return "Preview"
        for g in self._search.groups:
            if g.parent_id == self._preview.parent_id:
                name = Path(g.path).name
                if edge_width > 0:
                    prefix = "Preview — "
                    name = _elide_middle_keep_suffix(name, edge_width - 6 - len(prefix))
                return f"Preview — {name}"
        return "Preview"

    def _refresh_status(self) -> None:
        try:
            self.query_one("#results_pane", Tree).border_title = self._results.title()
            pane = self.query_one("#preview_pane")
            pane.border_title = self._preview_title(pane.region.width)
        except Exception:
            pass
        self._refresh_preview_match_indicator()
        self._refresh_footer_hints()
        # A new result set changes how many rows the results pane wants.
        self._reflow_sidebar()

    def _refresh_preview_match_indicator(self) -> None:
        """Show ``▲a ▼b`` on the preview's BOTTOM border (in the active-pane
        accent) counting how many screenfuls ("views") of the CURRENT result
        hold a match above / below the viewport. The results-pane arrows step
        between results and skip matches lower in the same chunk; this is the
        signal that such hidden matches exist, so the user knows to press n/b.
        Blank when the current result's matches all fit on screen, or in Reading
        View (which drops the border)."""
        try:
            pane = self.query_one("#preview_pane", MatchAwareScroll)
        except Exception:
            return
        nav = getattr(self, "_match_nav", None)
        if self._current_match_unlocatable() and not self._reading_mode:
            # The engine matched this chunk but the preview has nothing to
            # highlight. Say so rather than leaving the user to wonder why the
            # result they picked looks unrelated — and so a highlighting
            # regression is visible on screen. See fnd.tui.match_evidence.
            pane.border_subtitle = " [$warning]◌ match not shown here[/] "
        elif nav is not None and not self._reading_mode and (nav.above or nav.below):
            parts: list[str] = []
            if nav.above:
                parts.append(f"[$accent]▲{nav.above}[/]")
            if nav.below:
                parts.append(f"[$accent]▼{nav.below}[/]")
            pane.border_subtitle = f" {'  '.join(parts)} "
        else:
            pane.border_subtitle = ""

    def _current_match_unlocatable(self) -> bool:
        """True when the chunk the preview is showing for the current result
        carries no paintable match. Reads the decoded chunk, not the mounted
        widgets, so it is settle-independent — the answer is the same before
        and after the scroll lands.

        Gated on the anchor still being *armed*: the notice describes where the
        selected result put the view, so once the user scrolls away it no longer
        describes anything on screen. ``release()`` clears ``_armed`` but keeps
        ``_anchor`` for the controller's own bookkeeping, so reading the anchor
        alone left the notice pinned to the border for the rest of the session.
        """
        if not self._preview_scroll.is_armed:
            return False
        anchor = getattr(self._preview_scroll, "anchor", None)
        if anchor is None:
            return False
        chunks = self._preview.chunk_cache.get(anchor.parent_id)
        if not chunks:
            return False
        chunk = next((c for c in chunks if c.chunk_seq == anchor.focus_chunk_seq), None)
        if chunk is None:
            return False
        return not has_paintable_match(chunk, self._evidence_spec_for_current_row())

    def _evidence_spec_for_current_row(self) -> MatchSpec:
        """Evidence spec for the result the cursor is on, honouring which search
        pass produced it (see :func:`fnd.tui.match_evidence.evidence_spec_for_pass`).
        Falls back to the strict spec when the row can't be read."""
        pass_index = 0
        try:
            node = self.query_one("#results_pane", Tree).cursor_node
            data = node.data if node is not None else None
            if isinstance(data, dict) and data.get("kind") == "section":
                pass_index = int(data["hit"].pass_index)
        except Exception:
            pass_index = 0
        return evidence_spec_for_pass(
            pass_index,
            strict=self._effective_evidence_spec,
            painting=self._effective_match_spec,
        )

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
        # A background mount task can finalize and refresh the footer
        # after the app has begun tearing down — at which point the screen
        # stack is empty and ``self.focused`` (→ ``self.screen``) raises
        # ScreenStackError. No screen means no focus context to resolve.
        if not self.screen_stack:
            return "global"
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
            if wid in ("filters_panel_tree", "clear_filters_bar"):
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
            ("{alt_key}↑↓", "Skim"),
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

        # Match navigation key hint — shown in the keybinding area (like every
        # other key) whenever the current preview has matches. The k/N COUNT
        # itself lives on the preview's border, not here. Placed first so it
        # isn't clipped off the crowded footer line. Suppressed in Reading View,
        # where n/b are inert (like the border markers).
        nav = getattr(self, "_match_nav", None)
        if (
            nav is not None
            and overlay_hint is None
            and not self._reading_mode
            and ctx in ("results", "preview")
            and nav.current_chunk_has_stops()
        ):
            contextual = (("n/b", "Matches"), *contextual)

        with contextlib.suppress(Exception):
            self.query_one("#footer_hints", Static).update(
                render_hint_bar(self._FOOTER_ANCHORS, contextual)
            )

    # Maps a ``_focus_context`` result to the pane id that wears the accent
    # border. ``query``/``global`` map to nothing — no pane is accented.
    _FOCUS_BORDER_PANES: ClassVar[dict[str, str]] = {
        "results": "#results_pane",
        "collections": "#collections_panel_tree",
        "filters": "#filters_pane",
        "preview": "#preview_pane",
    }

    def on_descendant_focus(self) -> None:
        self._refresh_footer_hints()
        self._sync_focus_border()

    def _sync_focus_border(self) -> None:
        """Move the ``-focused`` accent border onto the logically-focused pane.

        A persistent class (not ``:focus-within``) so the border survives a
        terminal blur: Textual clears focus on ``AppBlur`` and only restores it
        on the next keypress, which made every pane's border vanish on tab-away
        and reappear a beat late. We only touch the class on a genuine focus
        move; ``AppBlur`` fires a descendant *blur*, not focus, so the class —
        and the border — stays put. ``set_class(update=False)`` + a per-pane
        ``stylesheet.apply`` keeps this off the subtree style-walk path."""
        # No screen (teardown mid-quit) → self.query_one would raise
        # ScreenStackError; bail like _focus_context does rather than mask it.
        if not self.screen_stack:
            return
        focused_id = self._FOCUS_BORDER_PANES.get(self._focus_context())
        for pane_id in self._FOCUS_BORDER_PANES.values():
            try:
                pane = self.query_one(pane_id)
            except NoMatches:
                continue
            should = pane_id == focused_id
            if should == ("-focused" in pane.classes):
                continue
            pane.set_class(should, "-focused", update=False)
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
        if tree is None or "collapsed" not in self._panel_frame(tree).classes:
            return
        try:
            column = self.query_one("#results_column", Vertical)
        except Exception:
            return
        # The filters tree is wrapped in #filters_pane, so it isn't a direct
        # child Tree; descend into non-Tree children to reach it, keeping
        # column order so the Up/Down sweep still visits every panel.
        panes: list[Tree[Any]] = []
        for child in column.children:
            if isinstance(child, Tree):
                panes.append(child)
            else:
                panes.extend(child.query(Tree).results())
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
        self._search.run(ev.value)

    def on_resize(self, _event: events.Resize) -> None:
        """Re-fit elided filenames to the new pane widths. Deferred to after
        layout so the panes report their settled geometry."""
        self.call_after_refresh(self._results.refit_after_resize)
        # A taller/shorter column changes every panel's fair share.
        self._reflow_sidebar()

    @on(Tree.NodeHighlighted)
    def _on_tree_highlight(self, ev: Tree.NodeHighlighted[Any]) -> None:
        # NodeHighlighted is a message, so it is handled a tick or more after
        # the cursor actually moved. Back-to-back searches each rebuild the
        # results tree and each post highlight events, so an earlier rebuild's
        # event can still be queued when a later one has already been handled.
        # Such an echo names a row the user has already left; loading it wins
        # the "last writer" race and settles the preview on a file the cursor
        # is not on. The cursor moving TO a node is what posts the event, so a
        # node that is no longer the cursor is stale by definition.
        tree = ev.node.tree
        if tree.cursor_node is not ev.node:
            return
        self._load_result_node(ev.node.data)

    @on(Tree.NodeSelected, "#results_pane")
    def _on_results_selected(self, ev: Tree.NodeSelected[Any]) -> None:
        # Enter loads the highlighted row even after an Option-scan (which
        # suppressed the per-row load): end scan mode and load it now, so a user
        # can browse with Option then press Enter to mount exactly what they
        # landed on. (NodeSelected stays unbound from opening the file — see the
        # note above.) Cancel any armed cooldown timer first so the load fires
        # immediately (leading edge) rather than waiting out the debounce window.
        self._preview._scan_move = False
        self._preview.cancel_pending_load()
        self._load_result_node(ev.node.data)

    def _load_result_node(self, data: Any) -> None:
        # Same cursor→target mapping the settle-time paint check reads, so the
        # loader and the check can never disagree about which file is selected.
        target = target_from_node_data(data)
        if target is None:
            return
        self._preview.schedule_load(*target)

    # ── Preview delegation (state lives on PreviewPresenter) ──────
    # Tests, sibling components, and the scroll strategies read AND
    # write these names on the app; the property pairs keep that
    # surface stable while the presenter owns the state.

    def _diag_log_path(self) -> str:
        """Opt-in preview-perf diagnostic log, in the OS temp dir. Gated by
        _FND_PREVIEW_DIAG=1; production code paths never write here."""
        import os
        import tempfile

        return os.path.join(tempfile.gettempdir(), "fnd-preview-diag.log")

    def _diag_log(self, msg: str) -> None:
        # _FND_PREVIEW_DIAG=1 appends to <tempdir>/fnd-preview-diag.log.
        # Investigation-only; remove once findings recorded.
        import os
        import time as _time

        if not os.environ.get("_FND_PREVIEW_DIAG"):
            return
        try:
            with open(self._diag_log_path(), "a", encoding="utf-8") as f:
                f.write(f"[{_time.monotonic():.3f}] {msg}\n")
        except Exception:
            pass

    def action_diag_dump_preview(self) -> None:
        # Walks the active preview and writes a per-type widget count
        # to /tmp/fnd-preview-diag.log. Always on (ignores the
        # env-var gate the log writes use) so a one-key tap works.
        from collections import Counter

        lines: list[str] = ["--- dump_preview ---"]
        active = self._preview.active
        flat = self._flat.active_buffer
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
        log_path = self._diag_log_path()
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception:
            pass
        self.notify(
            f"Dumped preview widget tree → {log_path}",
            timeout=2,
        )

    def _select_scroll_strategy(self) -> ScrollStrategy | None:
        """Pick the active preview's scroll strategy: the flat line-buffer when
        one is showing (PDF/TXT), else the structural per-chunk strategy."""
        if self._flat.active_buffer is not None:
            return self._preview_scroll_flat
        return self._preview_scroll_structural

    # ── Open dispatch ─────────────────────────────────────────────

    # Note: we deliberately do NOT bind Tree.NodeSelected to opener.open_smart.
    # Per user feedback, clicking / Enter should populate the preview only;
    # opening externally requires the explicit `o` (open at locator) or `O`
    # (open default app) bindings. Selection still fires NodeHighlighted
    # which drives the preview render via `_on_tree_highlight`.

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

    def action_reindex_default(self) -> None:
        """Convenience action: reindex the default collection."""
        try:
            self._indexer.reindex_with_warning("default")
        except Exception as e:
            self.notify(f"Could not start indexer: {e}", severity="error")

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
        target = self._results.target_for_node(tree.cursor_node)
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
            query=self._search.current_query,
            source=self._source_for_hit(hit),
        )

    def action_open_default_app(self) -> None:
        """Open the focused file in its default app, ignoring the locator."""
        tree = self.query_one("#results_pane", Tree)
        if tree.cursor_node is None:
            return
        target = self._results.target_for_node(tree.cursor_node)
        if target is None:
            return
        _, hit = target
        opener.open_default(Path(hit.path))

    def action_reveal_in_file_manager(self) -> None:
        """Show the focused result in the platform file manager, no app launch.
        Fire-and-forget via the launcher seam (macOS ``open -R`` · Windows
        ``explorer /select,`` · Linux file-manager ``--select``)."""
        tree = self.query_one("#results_pane", Tree)
        if tree.cursor_node is None:
            return
        target = self._results.target_for_node(tree.cursor_node)
        if target is None:
            return
        _, hit = target
        opener.reveal(Path(hit.path))

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
        target = self._results.target_for_node(tree.cursor_node)
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
        hit_with_query = _HitWithQuery(hit, self._search.current_query)
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
        active = self._scope.collections or list(cfg.collections.keys())
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

    def _panel_frame(self, tree: Tree[Any]) -> Widget:
        """The bordered / collapsible widget for ``tree``. The filters tree is
        wrapped in ``#filters_pane`` (which carries the border, title and
        collapse state); every other tree is its own frame."""
        if tree.id == "filters_panel_tree":
            with contextlib.suppress(NoMatches):
                return self.query_one("#filters_pane", Vertical)
        return tree

    def _reflow_sidebar(self, *, immediate: bool = False) -> None:
        """Recompute the sidebar panels' heights from live content demand.

        Coalesced to one pass per frame, so it's safe (and cheap) to call from
        any change that alters what the panels want to show — a new result set,
        a rebuilt filter/collection list, a section expanding or collapsing, a
        panel collapsing to its header, or a terminal resize. That breadth is
        the point: the split stays responsive to every one of those the way a
        static CSS rule can't. See :mod:`fnd.tui.sidebar_layout`.

        ``immediate`` runs the reallocation NOW, in the same message handler as
        the change, rather than after the next refresh. Use it when a panel
        toggles its ``collapsed`` class: deferring the reflow leaves the panel
        drawn at its old (taller) inline height for one frame — visible as a
        container with side walls but no bottom edge — before it snaps to the
        header box. Doing it inline clears the stale height in the same frame,
        so the collapse/expand is a single clean step. Content-demand reflows
        (new results, section folds) still defer, since their new
        ``virtual_size`` only settles after a refresh."""
        if not self.screen_stack:
            return
        if immediate:
            self._reflow_pending = False
            self._do_reflow_sidebar()
            return
        if self._reflow_pending:
            return
        self._reflow_pending = True
        self.call_after_refresh(self._do_reflow_sidebar)

    def _do_reflow_sidebar(self) -> None:
        self._reflow_pending = False
        if not self.screen_stack:
            return
        try:
            column = self.query_one("#results_column", Vertical)
            results = self.query_one("#results_pane", Tree)
            collections = self.query_one("#collections_panel_tree", Tree)
            filters_pane = self.query_one("#filters_pane", Vertical)
            filters_tree = self.query_one("#filters_panel_tree", Tree)
            clear_bar = self.query_one("#clear_filters_bar", Widget)
        except NoMatches:
            return
        avail = column.content_region.height
        if avail <= 0:
            return  # not laid out yet; a later trigger will reflow
        bar_rows = 1 if clear_bar.display else 0
        # demand = visible content rows + chrome (borders, and the docked clear
        # bar for the filters pane). virtual_size tracks the tree's expanded
        # rows, so it shrinks/grows as sections fold.
        specs: list[tuple[Widget, Panel]] = [
            (
                results,
                Panel(
                    "results_pane",
                    int(results.virtual_size.height) + 2,
                    "collapsed" in results.classes,
                    3,
                ),
            ),
            (
                collections,
                Panel(
                    "collections_panel_tree",
                    int(collections.virtual_size.height) + 2,
                    "collapsed" in collections.classes,
                    2,
                ),
            ),
            (
                filters_pane,
                Panel(
                    "filters_pane",
                    int(filters_tree.virtual_size.height) + 2 + bar_rows,
                    "collapsed" in filters_pane.classes,
                    2,
                ),
            ),
        ]
        heights = allocate(avail, [spec for _, spec in specs])
        for widget, spec in specs:
            if spec.collapsed:
                # The .collapsed CSS rule owns the header height; drop any stale
                # inline height so it takes effect (and reads back cleanly).
                if self._sidebar_height_cache.get(spec.key) != "collapsed":
                    widget.styles.height = None
                    self._sidebar_height_cache[spec.key] = "collapsed"
            else:
                target = heights[spec.key]
                if self._sidebar_height_cache.get(spec.key) != target:
                    widget.styles.height = target
                    self._sidebar_height_cache[spec.key] = target

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
        frame = self._panel_frame(tree)
        if "collapsed" in frame.classes:
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
                if frame.id:
                    frame.add_class("collapsed")
                    self._scope.collapsed_panels.add(frame.id)
                    self._scope.persist()
                    self._reflow_sidebar(immediate=True)  # collapse in one frame
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
        frame = self._panel_frame(tree)
        if "collapsed" in frame.classes:
            frame.remove_class("collapsed")
            if frame.id:
                self._scope.collapsed_panels.discard(frame.id)
                self._scope.persist()
            self._reflow_sidebar(immediate=True)  # expand in one frame
            return
        node = tree.cursor_node
        if node is None or not node.children:
            if node is not None and tree.id == "results_pane":
                self.action_focus_preview_pane()
            return
        if not node.is_expanded:
            node.expand()
        self._move_cursor_to_first_child(tree, node)

    def action_tree_expand_all_children(self) -> None:
        """Expand the focused node and reveal its whole subtree — the node plus
        every descendant. Works in every focusable sidebar tree (results,
        collections, filters)."""
        tree = self._focused_tree()
        if tree is None:
            return
        node = tree.cursor_node
        if node is not None and node.children:
            node.expand_all()

    def action_tree_collapse_all_children(self) -> None:
        """Collapse the focused node's *children* (each child and its subtree),
        leaving the node itself open. Only the descendants fold away — the row
        you're on stays put, so this tidies a deep subtree without backing out
        of it (use the plain Left arrow to collapse the node itself)."""
        tree = self._focused_tree()
        if tree is None:
            return
        node = tree.cursor_node
        if node is None:
            return
        for child in node.children:
            child.collapse_all()

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

    @on(ResultsTree.ReopenRequested)
    def _on_results_reopen_requested(self, ev: ResultsTree.ReopenRequested) -> None:
        """A click on a collapsed-to-header panel reopens it and expands the
        clicked result — the ``Right``-arrow reopen (``action_tree_smart_expand``)
        for the mouse, surfacing the specific row the user pointed at rather
        than toggling a node hidden behind the collapsed height."""
        tree = ev.tree
        # The collapse state lives on the panel frame — the tree itself for the
        # results/collections panels, but the #filters_pane wrapper for the
        # filters tree — so resolve it the same way the keyboard reopen does.
        if not self._reopen_panel_frame(tree):
            return
        node = ev.node
        if node is None:
            return
        if node.allow_expand and not node.is_expanded:
            node.expand()
            self._move_cursor_to_first_child(tree, node)
        else:
            tree.move_cursor(node)

    def _reopen_panel_frame(self, tree: Tree[Any]) -> bool:
        """Un-collapse ``tree``'s panel frame if it is collapsed-to-header.
        Returns whether a reopen actually happened."""
        frame = self._panel_frame(tree)
        if "collapsed" not in frame.classes:
            return False
        frame.remove_class("collapsed")
        if frame.id:
            self._scope.collapsed_panels.discard(frame.id)
            self._scope.persist()
        self._reflow_sidebar(immediate=True)  # reopen in one frame
        return True

    @on(events.Click, "#filters_pane")
    def _on_filters_pane_click(self, event: events.Click) -> None:
        """Click-to-reopen for the filters pane. Its tree is wrapped in
        #filters_pane, so at header height the tree has zero height and never
        sees the click (unlike results/collections, whose collapse class sits on
        the tree itself). Catch it on the container and reopen the frame."""
        filters_tree = self.query_one("#filters_panel_tree", Tree)
        if self._reopen_panel_frame(filters_tree):
            event.stop()
            filters_tree.focus()

    @property
    def _effective_match_spec(self) -> MatchSpec:
        """The MatchSpec the renderers should consult. Falls back to an
        empty spec when the user has toggled highlights off — the
        preview pane then renders the plain document with no yellow /
        orange overlays and no scrollbar match markers."""
        return self._search.match_spec if self._search.highlights_enabled else MatchSpec()

    @property
    def _effective_evidence_spec(self) -> MatchSpec:
        """The MatchSpec that decides whether a result can show the user their
        match (:mod:`fnd.tui.match_evidence`).

        Same as :meth:`_effective_match_spec` but without AUTO-fuzzy. Painting
        is deliberately generous — a query for "test" highlights "best" and
        "rest" at edit distance 1 so a typo still lands somewhere. Those are not
        evidence: judging visibility by them reported "your match is here" over
        a paragraph containing nothing the user typed, which is precisely the
        complaint this work exists to fix.
        """
        return self._search.evidence_spec if self._search.highlights_enabled else MatchSpec()

    def action_toggle_highlights(self) -> None:
        self._search.toggle_highlights()

    def action_toggle_fuzzy(self) -> None:
        self._search.toggle_fuzzy()

    def action_nav_next_match(self) -> None:
        # Reading View is pure scroll-nav — no result-driven match jumps there.
        if self._reading_mode:
            return
        self._match_nav.next()

    def action_nav_prev_match(self) -> None:
        if self._reading_mode:
            return
        self._match_nav.prev()

    def action_focus_results_pane(self) -> None:
        """Single-key teleport from anywhere → results tree."""
        self.query_one("#results_pane", Tree).focus()

    def action_focus_preview_pane(self) -> None:
        """Single-key teleport from anywhere → preview pane."""
        self.query_one("#preview_pane").focus()

    def action_clear_filters(self) -> None:
        """Reset every active filter (file type, dates, tags) to default.
        Collection/source scope is untouched. No-op when nothing is active."""
        self._scope.clear_filters()

    def action_copy_query_command(self) -> None:
        """Copy the current query + active filters to the clipboard as a
        runnable ``fnd`` command, so the search can be saved and re-run."""
        from fnd.tui.clipboard import copy_text

        result = LaunchCommandSerializer(
            self._scope.snapshot(self._search.current_query)
        ).serialize()
        if result.is_empty:
            self.notify("Nothing to copy — type a query or set a filter first.", severity="warning")
            return
        try:
            copy_text(result.command)
        except OSError as e:
            self.notify(f"Could not copy: {e}", severity="error")
            return
        message = "Copied command to clipboard"
        if result.caveats:
            message += f" ({'; '.join(result.caveats)})"
        self.notify(message, timeout=3)

    def action_focus_filters_panel(self) -> None:
        """Single-key teleport from anywhere → filters sidebar panel."""
        self.query_one("#filters_panel_tree", Tree).focus()

    def action_focus_collections_panel(self) -> None:
        """Single-key teleport from anywhere → collections sidebar panel."""
        self.query_one("#collections_panel_tree", Tree).focus()

    @on(events.Click, "#clear_filters_bar")
    def _on_clear_bar_click(self, event: events.Click) -> None:
        event.stop()
        self._scope.clear_filters()

    @on(Tree.NodeSelected, "#filters_panel_tree")
    def _on_filters_panel_selected(self, ev: Tree.NodeSelected[dict[str, object]]) -> None:
        ev.stop()
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

    # A section (branch) folding open or closed in any sidebar tree changes how
    # many rows that panel wants — reflow so the shares track it live.
    _SIDEBAR_TREE_IDS: ClassVar[frozenset[str]] = frozenset(
        {"results_pane", "collections_panel_tree", "filters_panel_tree"}
    )

    @on(Tree.NodeExpanded)
    def _reflow_on_node_expanded(self, ev: Tree.NodeExpanded[Any]) -> None:
        if ev.node.tree.id in self._SIDEBAR_TREE_IDS:
            self._reflow_sidebar()

    @on(Tree.NodeCollapsed)
    def _reflow_on_node_collapsed(self, ev: Tree.NodeCollapsed[Any]) -> None:
        if ev.node.tree.id in self._SIDEBAR_TREE_IDS:
            self._reflow_sidebar()

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
        if self._search.latest_trace is None:
            self.notify(
                "no search yet — type a query first",
                severity="warning",
                title="Explain",
            )
            return
        import json

        from textual.widgets import Markdown as _Md

        body = json.dumps(self._search.latest_trace.to_json(), indent=2)
        md = (
            f"# Explain — `{self._search.latest_trace.query}`\n\n"
            f"Regime: **{self._search.latest_trace.regime}**\n\n"
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
        result = parse_multi_input(text, synonyms=self._search.synonyms)
        self._search.intent = result.intent
        # Use lex line(s) as the search query (auto_subqueries inside
        # fusion_search will re-derive phrase + syn from this). Keeping
        # intent-only as the UX-pass-4 §3 hook; explicit sub-query
        # override is a future extension.
        lexical_parts = [s.query for s in result.subqueries if s.source == "lex"]
        if lexical_parts:
            self._search.run(" ".join(lexical_parts))

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
        self._search.ranking_profile = self._search.resolve_profile()
        self._refresh_status()
        self._scope.refresh_collections_panel()
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
                self._search.ranking_profile = self._search.resolve_profile()
                self._refresh_status()
                self._scope.refresh_collections_panel()
            except Exception:
                pass

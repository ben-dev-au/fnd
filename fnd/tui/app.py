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
import re
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from rich.text import Text

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.content import Span
from textual.scrollbar import ScrollBar
from textual.widget import Widget
from textual.widgets import (
    Input,
    Markdown,
    Static,
    Tree,
)
from textual.widgets._markdown import (
    MarkdownBlock,
    MarkdownBlockQuote,
    MarkdownH1,
    MarkdownH2,
    MarkdownH3,
    MarkdownH4,
    MarkdownH5,
    MarkdownH6,
    MarkdownOrderedListItem,
    MarkdownParagraph,
    MarkdownTable,
    MarkdownTD,
    MarkdownTH,
    MarkdownUnorderedListItem,
)
from textual.widgets.tree import TreeNode

from fnd import opener
from fnd.config import Config, default_index_dir
from fnd.explain import SearchTrace
from fnd.matching import MatchSpec
from fnd.query import FileChunk, FileGroup, Hit, Searcher
from fnd.render import (
    render_chunk_pieces,
    word_highlight_runs,
)
from fnd.rerank import RankingProfile, profile_from_config
from fnd.tui.actions import REGISTRY, Keymap, load_keymap
from fnd.tui.indexer_modal import IndexerScreen, drive_indexer
from fnd.tui.line_buffer import (
    FileView,
    LineBufferPreview,
    RenderedDocument,
    build_file_view,
    build_rendered_document,
)
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

# App-wide thin scrollbars: every stock Textual ScrollBar (results/sidebar
# trees, code fences, settings lists) renders the thumb as a hairline glyph
# hugging the frame instead of a reverse-video full-cell block. The preview's
# MatchAwareScrollBar applies the same thinning via its own renderer subclass.
ScrollBar.renderer = ThinScrollBarRender

_PASS_GLYPHS = {0: "●", 1: "~", 2: "⊕", 3: "❝"}


# Preview widget cache. Repeat visits to a previously-loaded file
# should be instant — keep the mounted widget tree alive in a per-file
# Container; switching files is then a single class-toggle. LRU-bounded.
# See docs/PREVIEW_DOM_PLAN.md for the planned rework that aims to make
# this cap effectively unlimited via screen-per-file isolation.
_PREVIEW_CACHE_MAX_FILES = 4
_PREVIEW_CACHE_MIN_CHUNKS = 1
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


class ResultsTree(Tree[dict[str, Any]]):
    """Results tree where expanded parents (file rows) are literally
    unselectable.

    Earlier the rule was enforced after-the-fact by ``_on_tree_highlight``
    and ``_bounce_after_expand``: the cursor would land on the parent row
    for a frame and then bounce. With a slow preview load on top, the
    bounce became visible and felt like a glitchy jump.

    Validating in :meth:`validate_cursor_line` is atomic — the cursor
    never lands on an expanded parent in the first place. Pressing ↓
    from the row above an expanded parent moves directly to the parent's
    first child; pressing ↑ from a child moves directly to the row above
    the parent. No frames in between.
    """

    def validate_cursor_line(self, value: int) -> int:
        clamped = super().validate_cursor_line(value)
        if not getattr(self, "_skip_expanded_parents", False):
            return clamped
        # Walk in the move direction past any expanded parents.
        current = int(self.cursor_line)
        direction = 1 if clamped > current else (-1 if clamped < current else 1)
        last = max(0, len(self._tree_lines) - 1)
        target = clamped
        safety = 0
        while safety < 64:
            try:
                line = self._tree_lines[target]
            except IndexError:
                return clamped
            node = line.node
            if node is self.root:
                return target
            if not (node.children and node.is_expanded):
                return target
            next_target = target + direction
            if next_target < 0 or next_target > last:
                # Boundary — don't shove the cursor off the edge; keep
                # it where it was so the press feels like a no-op
                # instead of a jump.
                return current
            target = next_target
            safety += 1
        return current


class PreviewContainer(Container):
    """Per-file preview container holding the mounted chunk widgets.

    One container per cached file lives inside ``#preview_pane``; only
    one is ``-active`` (visible) at a time. Switching files toggles
    classes — no remount cost. Each container tracks which chunk
    indices have been mounted so a partial-then-cancelled mount can be
    resumed on revisit.
    """

    # display:none required; visibility:hidden leaves containers in
    # vertical flow and collapses the active LineBufferPreview height.
    DEFAULT_CSS = """
    PreviewContainer { width: 100%; height: auto; }
    PreviewContainer.-hidden { display: none; }
    /* opacity:0 (not visibility:hidden) so the pane can be scrolled to the
       match WHILE the container is invisible — scroll_to_region is a no-op
       under visibility:hidden, which forced a reveal-at-top-then-scroll jump.
       The reveal lands only after the scroll commits (see _finalize_via_lock). */
    PreviewContainer.-pre-reveal { opacity: 0%; }
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
        self.chunk_widgets: dict[int, Widget] = {}
        # chunk_seq → first match-bearing widget (or header when no match).
        self.match_targets: dict[int, Widget] = {}

    @property
    def is_complete(self) -> bool:
        return len(self.mounted_indices) >= self.total_chunks

    def get_content_height(self, container, viewport, width):  # type: ignore[no-untyped-def]
        # Textual's VerticalLayout has an "all children are dynamic
        # height" shortcut that arranges them inside ``container.height``
        # — fine for flex-style nested containers, wrong here because
        # this widget is the scrollable canvas of #preview_pane. The
        # shortcut caps total height at the pane's viewport height
        # (~19 cells), so chunks past that y are positioned in widget
        # coords but unreachable via scroll. Force the non-shortcut
        # branch by arranging with height=0, which lets each FNDMarkdown
        # child report its full intrinsic height.
        if not self._nodes:
            return 0
        from textual.geometry import Size as _Size

        arrangement = self.arrange(_Size(width, 0))
        return arrangement.total_region.height


class _HitWithQuery:
    """Adapter exposing a Hit plus the current query string as one object
    for ``OpenWithScreen``. The modal's :func:`build_request` reads
    ``hit.query`` to populate the Skim ``&search=`` highlight; Hit
    itself doesn't carry the query."""

    __slots__ = ("_hit", "query")

    def __init__(self, hit: Any, query: str) -> None:
        self._hit = hit
        self.query = query

    def __getattr__(self, name: str) -> Any:
        # Forward everything not explicitly overridden to the wrapped Hit.
        return getattr(self._hit, name)


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

    def put(
        self,
        container: PreviewContainer,
        *,
        protect: PreviewContainer | None = None,
    ) -> list[PreviewContainer]:
        """Cache ``container`` and return any LRU-evicted containers
        for the caller to remove. ``protect`` is skipped during eviction
        so prefetch can't drop the currently-active preview."""
        if container.total_chunks < self.min_chunks:
            return []
        key = (container.parent_doc_id, container.query_signature)
        self._cache[key] = container
        self._cache.move_to_end(key)
        evicted: list[PreviewContainer] = []
        while len(self._cache) > self.max_files:
            for k, old in self._cache.items():
                if old is protect:
                    continue
                evicted.append(old)
                del self._cache[k]
                break
            else:
                break
        return evicted

    def clear(self) -> list[PreviewContainer]:
        """Drop everything; return the previously-cached containers so
        the caller can remove them from the DOM."""
        evicted = list(self._cache.values())
        self._cache.clear()
        return evicted


# ── Highlight-aware Markdown widget tree ──────────────────────────────
#
# Textual's stock Markdown widget renders a per-block widget tree out
# of markdown-it tokens (headings, paragraphs, lists, blockquotes,
# tables, fenced code, etc.). We subclass the block kinds whose inline
# text should carry search-term highlights and overlay
# ``Content.add_spans`` after the base build runs — match-only spans
# layered on top of whatever style the base block produced. Code
# fences (``MarkdownFence``) are intentionally NOT subclassed: the
# stock widget uses ``rich.syntax.Syntax`` for the fence body, and
# splatting yellow highlights inside code makes it harder to read,
# not easier.
#
# The match logic shells out to the same ``_terms_from_query`` /
# ``_term_stems`` / Snowball stemmer used everywhere else in the app
# (fnd/render.py:46) so the highlight semantics agree with snippet
# detection and the per-line plain renderer.


def _build_match_spans(plain: str, spec: MatchSpec) -> list[Span]:
    """Return a list of highlight spans covering every word in ``plain``
    that matches ``spec`` under any of the cascade's pass semantics —
    exact-stem (literal / phrase / synonym) or fuzzy-AUTO.

    Char-level colour split: literal / synonym matches → one yellow
    span covering the whole word. Fuzzy-only matches → multiple spans
    split by Levenshtein alignment against the closest typed query
    term (yellow for chars that align, orange for substitutions /
    insertions). Same per-word run helper as the per-line plain
    renderer (``fnd.render.word_highlight_runs``) so the visual
    treatment is identical across markdown / docx / pptx / pdf / txt
    previews. Span styles are concrete Rich style strings so the
    visual doesn't depend on Textual's component-class CSS resolution.
    """
    if spec.is_empty or not plain:
        return []
    spans: list[Span] = []
    for m in re.finditer(r"\w+", plain):
        runs = word_highlight_runs(m.group(0), spec)
        for offset_start, offset_end, style in runs:
            spans.append(Span(m.start() + offset_start, m.start() + offset_end, style))
    return spans


def _record_first_match(block: MarkdownBlock, spans: list[Span]) -> None:
    """If this block contains the first highlighted match in the
    document, register it on the parent ``FNDMarkdown`` so the
    preview pane can scroll to it. First-write-wins: subsequent matched
    blocks don't overwrite.
    """
    if not spans:
        return
    md = block._markdown  # weakref unwrap
    if isinstance(md, FNDMarkdown) and md._first_match_block is None:
        md._first_match_block = block


def _apply_highlights_after_build(block: MarkdownBlock) -> None:
    """Common ``build_from_token`` postlude shared by every highlight-
    aware subclass: pull ``match_spec`` off the parent FNDMarkdown,
    compute spans against ``block._content.plain``, and replace the
    block's content with the span-augmented version. No-op when the
    parent isn't an FNDMarkdown (e.g. the stock Markdown widget the
    help overlay uses) or when the spec is empty."""
    md = block._markdown
    spec = getattr(md, "match_spec", None)
    if spec is None or spec.is_empty:
        return
    spans = _build_match_spans(block._content.plain, spec)
    if not spans:
        return
    block.set_content(block._content.add_spans(spans))
    _record_first_match(block, spans)


class _HighlightingBlockMixin:
    """Drop-in mixin for the MarkdownBlock subclasses that should apply
    search-term highlights after the base build. Avoids repeating the
    same five-line ``build_from_token`` body on every subclass."""

    def build_from_token(self, token):  # type: ignore[override]
        super().build_from_token(token)  # type: ignore[misc]
        _apply_highlights_after_build(self)  # type: ignore[arg-type]


class _HeadingMarkerMixin:
    """Prepend the level marker (``#`` / ``##`` / ``###`` …) to a
    heading's rendered content so users can distinguish heading levels
    in a terminal that can't render font-size differences. The marker
    inherits the heading's own color so it reads as a low-key prefix,
    not a second style band.

    Must sit *inside* ``_HighlightingBlockMixin`` in the MRO so the
    marker lands on the Content **before** highlight spans run — that
    way the highlight span offsets line up with the post-prefix plain.
    """

    def build_from_token(self, token):  # type: ignore[override]
        super().build_from_token(token)  # type: ignore[misc]
        from textual.content import Content

        marker = ("#" * self.LEVEL) + " "  # type: ignore[attr-defined]
        self.set_content(Content.assemble(marker, self._content))  # type: ignore[attr-defined]


class FNDMarkdownH1(_HighlightingBlockMixin, _HeadingMarkerMixin, MarkdownH1):
    pass


class FNDMarkdownH2(_HighlightingBlockMixin, _HeadingMarkerMixin, MarkdownH2):
    pass


class FNDMarkdownH3(_HighlightingBlockMixin, _HeadingMarkerMixin, MarkdownH3):
    pass


class FNDMarkdownH4(_HighlightingBlockMixin, _HeadingMarkerMixin, MarkdownH4):
    pass


class FNDMarkdownH5(_HighlightingBlockMixin, _HeadingMarkerMixin, MarkdownH5):
    pass


class FNDMarkdownH6(_HighlightingBlockMixin, _HeadingMarkerMixin, MarkdownH6):
    pass


class FNDMarkdownParagraph(_HighlightingBlockMixin, MarkdownParagraph):
    pass


class FNDMarkdownBlockQuote(_HighlightingBlockMixin, MarkdownBlockQuote):
    pass


class FNDMarkdownOrderedListItem(_HighlightingBlockMixin, MarkdownOrderedListItem):
    pass


class FNDMarkdownUnorderedListItem(_HighlightingBlockMixin, MarkdownUnorderedListItem):
    pass


class FNDMarkdownTH(_HighlightingBlockMixin, MarkdownTH):
    pass


class FNDMarkdownTD(_HighlightingBlockMixin, MarkdownTD):
    pass


class FNDMarkdownTableDT(MarkdownTable):
    """W3 prototype: render a markdown table as a single DataTable
    widget instead of ~N MarkdownTableCellContents widgets.

    Gated by ``_FND_W3_DATATABLE=1`` (off => parent's compose runs).

    Styling matches Textual's MarkdownTable as closely as DataTable
    allows: rounded outer border + zebra rows + primary-coloured bold
    header. The inter-cell keylines that grid-layout MarkdownTableContent
    gets aren't reachable on DataTable — it renders cells as a single
    render output, not as keyline-eligible child widgets.
    """

    DEFAULT_CSS = """
    FNDMarkdownTableDT {
        & > DataTable {
            border: round $foreground 20%;
            margin: 0 0 1 0;
            & > .datatable--header {
                text-style: bold;
                background: $panel;
                color: $primary;
            }
            & > .datatable--even-row {
                background: $panel 30%;
            }
        }
    }
    """

    def compose(self):  # type: ignore[override]
        # Opt out with _FND_NO_W3=1 to fall back to widget-per-cell.
        import os

        if os.environ.get("_FND_NO_W3") == "1":
            yield from super().compose()
            return
        from textual.containers import VerticalScroll
        from textual.coordinate import Coordinate
        from textual.widgets import DataTable

        headers, rows = self._get_headers_and_rows()
        self._headers = headers
        self._rows = rows
        header_texts = [_content_to_text(h) for h in headers]
        row_texts = [[_content_to_text(c) for c in row] for row in rows if row]
        dt: DataTable[Any] = DataTable(cursor_type="none", zebra_stripes=True)
        # Compute per-column widths from the pane's content size so wide
        # cells wrap rather than overflow. Without this, DataTable's
        # auto_width measures each column at its longest single line —
        # paragraph cells produce ~700-cell columns that get truncated
        # to the pane's 91 cells, with no wrap.
        try:
            pane = self.app.query_one("#preview_pane", VerticalScroll)
            # -1 for scrollbar, -2 for DataTable's outer border.
            avail = max(0, pane.content_size.width - 3)
        except Exception:
            avail = 0
        if avail <= 0:
            # Fallback: app width minus the results-pane column budget.
            avail = max(40, self.app.size.width - 52)
        col_widths = _compute_table_col_widths(
            header_texts, row_texts, available_width=avail, cell_padding=dt.cell_padding
        )
        if header_texts and col_widths and len(col_widths) == len(header_texts):
            for label, w in zip(header_texts, col_widths, strict=True):
                dt.add_column(label, width=w)
        elif header_texts:
            dt.add_columns(*header_texts)
        for row in row_texts:
            dt.add_row(*row, height=None)
        md = self._markdown
        spec = getattr(md, "match_spec", None) or MatchSpec()
        match_coord = _find_first_match_coord_in_table(headers, rows, spec)
        if match_coord is not None:
            dt._fnd_match_coord = Coordinate(*match_coord)  # type: ignore[attr-defined]
            # Register self as parent's first_match_block — TH/TD
            # widgets are bypassed so _record_first_match never fires.
            if isinstance(md, FNDMarkdown) and md._first_match_block is None:
                md._first_match_block = self
        yield dt


def _content_to_text(c: Any) -> Text:
    """Convert textual.Content → rich.Text, preserving highlight spans."""
    from rich.text import Text

    plain = getattr(c, "plain", None)
    if plain is None:
        return Text(str(c))
    t = Text(plain)
    spans = getattr(c, "spans", None) or ()
    for span in spans:
        try:
            t.stylize(str(span.style), span.start, span.end)
        except Exception:
            continue
    return t


def _compute_table_col_widths(
    header_texts: list[Text],
    row_texts: list[list[Text]],
    *,
    available_width: int,
    cell_padding: int = 1,
    min_floor: int = 4,
) -> list[int]:
    """Min-content floor + proportional remainder column distribution.

    Each column's min = longest unsplittable word (clamped to ``min_floor``),
    max = longest single line. If the natural max-widths fit, return them.
    Otherwise give every column its min, then split remaining space across
    columns proportional to each column's ``(max - min)`` demand. Padding
    is subtracted from ``available_width`` before distribution since
    DataTable adds ``cell_padding`` to each side of every cell at render.
    """
    n_cols = len(header_texts)
    if n_cols == 0 or available_width <= 0:
        return []

    def _longest_word(s: str) -> int:
        return max((len(w) for w in s.split()), default=0)

    def _longest_line(s: str) -> int:
        return max((len(line) for line in (s.splitlines() or [s])), default=0)

    mins = [0] * n_cols
    maxs = [0] * n_cols
    for col_idx, h in enumerate(header_texts):
        plain = h.plain
        mins[col_idx] = max(mins[col_idx], _longest_word(plain))
        maxs[col_idx] = max(maxs[col_idx], _longest_line(plain))
    for row in row_texts:
        for col_idx, cell in enumerate(row[:n_cols]):
            plain = cell.plain
            mins[col_idx] = max(mins[col_idx], _longest_word(plain))
            maxs[col_idx] = max(maxs[col_idx], _longest_line(plain))
    mins = [max(min_floor, m) for m in mins]

    inner_avail = available_width - n_cols * (2 * cell_padding)
    if inner_avail <= 0:
        return mins
    if sum(maxs) <= inner_avail:
        return [max(m, mn) for m, mn in zip(maxs, mins, strict=True)]
    total_min = sum(mins)
    if total_min >= inner_avail:
        # Mins alone exceed budget — scale every column down proportionally
        # (loses some content, but the only alternative is overflow).
        scale = inner_avail / total_min
        return [max(1, int(m * scale)) for m in mins]
    widths = list(mins)
    remaining = inner_avail - total_min
    demand = [max(0, mx - mn) for mx, mn in zip(maxs, mins, strict=True)]
    total_demand = sum(demand)
    if total_demand > 0:
        for i in range(n_cols):
            widths[i] += int(remaining * demand[i] / total_demand)
        leftover = inner_avail - sum(widths)
        if leftover > 0:
            widest = max(range(n_cols), key=lambda i: demand[i])
            widths[widest] += leftover
    return widths


def _find_first_match_coord_in_table(
    headers: list[Any], rows: list[list[Any]], spec: MatchSpec
) -> tuple[int, int] | None:
    """Return (row, col) of the first cell that contains a query match.

    A cell matches iff one of its words matches ``spec`` — the same
    ``word_matches`` gate the highlight overlay applies — so the
    coordinate always points at a cell that is actually highlighted.
    (Checking the Content's ``spans`` instead is wrong: that set also
    carries the markdown styling spans — inline code, emphasis, links —
    so the first *styled* cell wins over the first *matched* one, parking
    the scroll near the table top while the real match sits rows below.)
    ``text_has_any_match`` short-circuits on the first matching word and
    skips the per-char alignment / Span allocation that building the full
    highlight spans here would waste on every cell of a large table.

    Header hits map to row 0 col c as a best-effort approximation since
    the DataTable cursor doesn't address headers directly.
    """
    from fnd.render import text_has_any_match

    if spec.is_empty:
        return None
    for col, h in enumerate(headers):
        if text_has_any_match(getattr(h, "plain", "") or "", spec):
            return (0, col)
    for r_idx, row in enumerate(rows):
        for c_idx, cell in enumerate(row):
            if text_has_any_match(getattr(cell, "plain", "") or "", spec):
                return (r_idx, c_idx)
    return None


class FNDMarkdown(Markdown):
    """Markdown widget with inline search-term highlighting.

    Subclasses ``textual.widgets.Markdown`` and registers
    highlight-aware block subclasses for the kinds whose inline text
    should carry the highlight overlay (headings, paragraphs,
    blockquotes, list items, table cells). Fenced code blocks
    (``MarkdownFence``) intentionally remain on the base class — the
    stock widget renders them via ``rich.syntax.Syntax`` and we don't
    want to muddy that with extra styling.

    The user's query stems are passed in at construction time and
    stashed on the instance so each block subclass can read them
    during ``build_from_token``. ``first_match_block`` resolves to the
    earliest block in document order whose Content gained at least
    one highlight span — the preview pane scrolls to it so the user
    sees the match without manual scrolling.
    """

    DEFAULT_CSS = """
    FNDMarkdown {
        height: auto;
    }
    /* All six heading levels render in the theme accent colour, not just
       H1-H3 (Textual's stock palette stops colouring at H3 and falls back
       to plain text-style on H4-H6). A terminal can't show font-size
       differences, so the level marker prefix ("#" / "##" / "###" …) is
       baked into the heading content by ``_HeadingMarkerMixin`` to give
       readers the level cue. Bold / underline are layered on top so the
       top three levels still feel weightier without changing colour. */
    FNDMarkdown FNDMarkdownH1 { color: $accent; text-style: bold; }
    FNDMarkdown FNDMarkdownH2 { color: $accent; text-style: bold underline; }
    FNDMarkdown FNDMarkdownH3 { color: $accent; text-style: bold; }
    FNDMarkdown FNDMarkdownH4 { color: $accent; text-style: underline; }
    FNDMarkdown FNDMarkdownH5 { color: $accent; text-style: none; }
    FNDMarkdown FNDMarkdownH6 { color: $accent 70%; text-style: none; }
    /* Inline emphasis colour-shifts too — text-style alone is too
       subtle to read at terminal weight on most fonts. ``$primary``
       contrasts with ``$accent`` (headings) so bold inside a heading
       is still visible. */
    FNDMarkdown MarkdownBlock > .strong { color: $primary; text-style: bold; }
    FNDMarkdown MarkdownBlock > .em { color: $secondary; text-style: italic; }
    """

    BLOCKS: dict[str, type[MarkdownBlock]] = {  # noqa: RUF012
        **Markdown.BLOCKS,
        "h1": FNDMarkdownH1,
        "h2": FNDMarkdownH2,
        "h3": FNDMarkdownH3,
        "h4": FNDMarkdownH4,
        "h5": FNDMarkdownH5,
        "h6": FNDMarkdownH6,
        "paragraph_open": FNDMarkdownParagraph,
        "blockquote_open": FNDMarkdownBlockQuote,
        "list_item_ordered_open": FNDMarkdownOrderedListItem,
        "list_item_unordered_open": FNDMarkdownUnorderedListItem,
        "th_open": FNDMarkdownTH,
        "td_open": FNDMarkdownTD,
        "table_open": FNDMarkdownTableDT,
    }

    def __init__(
        self,
        markdown: str | None = None,
        *,
        match_spec: MatchSpec | None = None,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        import asyncio as _asyncio

        super().__init__(markdown=markdown, name=name, id=id, classes=classes)
        self.match_spec: MatchSpec = match_spec or MatchSpec()
        self._first_match_block: MarkdownBlock | None = None
        # Set by ``_on_mount`` after ``super()._on_mount`` (which awaits
        # ``Markdown.update``) returns. Lets the scroll path event-trigger
        # on build completion instead of polling.
        self.build_done: _asyncio.Event = _asyncio.Event()

    @property
    def first_match_block(self) -> MarkdownBlock | None:
        """The first highlighted block in document order, or ``None``
        when the source has no matches. Set by the highlight-aware
        block subclasses during ``build_from_token``."""
        return self._first_match_block

    def update(self, markdown):  # type: ignore[no-untyped-def, override]
        # Textual's dispatcher walks the MRO and invokes every class's
        # _on_mount — overriding _on_mount and calling super() ran
        # Markdown._on_mount twice; the second pass saw _initial_markdown
        # already consumed and called update("") which removed all
        # blocks. Hook into update() instead: AwaitComplete's future
        # fires when parse+mount completes — set build_done from there.
        aw = super().update(markdown)
        aw._future.add_done_callback(lambda _: self.build_done.set())  # type: ignore[attr-defined]
        return aw


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


def _styled_parent_label(label: Any) -> Any:
    """Render a tree-parent label in the muted "structural row" style.

    Parents in the Results and Filters trees aren't cursor-selectable
    when expanded (`_skip_expanded_parents`); Collections parents stay
    selectable but get the same visual treatment so the parent/child
    distinction reads consistently across all three trees.
    """
    from rich.text import Text

    if isinstance(label, Text):
        styled = label.copy()
        styled.stylize("dim")
        return styled
    return Text(str(label), style="dim")


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


# Per-chunk renderer choice lives in ``preview_dispatcher`` so the
# file-level ``choose_preview_mode`` decision and the per-chunk mount
# loop can't drift apart on which kinds are markdown-rendered.
_uses_markdown_renderer = uses_markdown_renderer


def _legacy_blocks_to_md(blocks: list[Any]) -> str:
    """Reconstruct a minimal markdown source from the legacy plain-text
    Block list. Used as a defensive fallback for chunks with empty
    ``body_md`` (e.g. an old index that slipped through the schema
    check). Never produces structurally-correct markdown for tables /
    nested lists / fenced code — those round-tripping properly is
    exactly what the schema bump and extractor rewrite are for. Just
    enough to keep a stale-index preview from rendering empty.
    """
    parts: list[str] = []
    for b in blocks:
        kind = getattr(b, "kind", "p")
        text = getattr(b, "text", "") or ""
        if kind in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = int(kind[1])
            parts.append(f"{'#' * level} {text}\n")
        else:
            parts.append(f"{text}\n\n")
    return "".join(parts).strip() + "\n"


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


def _elide_middle_keep_suffix(name: str, max_width: int) -> str:
    """Middle-truncate ``name`` to ``max_width`` chars, keeping the extension
    visible: ``really_long_report_final_v3.pdf`` -> ``really_…nal_v3.pdf``.

    A terminal's default right-clip drops the extension — the one part that
    says what kind of file it is — so we elide the stem's middle and keep both
    ends plus the suffix. When even one stem char won't fit we still show
    ``…<suffix>``; only when the suffix itself can't fit (``max_width`` shorter
    than ``…`` + suffix) do we fall back to a plain right-truncation that drops
    it. Char-counted (like ``_shorten``); wide glyphs aside.
    """
    if len(name) <= max_width:
        return name
    if max_width <= 1:
        return name[: max(0, max_width)]
    suffix = Path(name).suffix
    stem = name[: len(name) - len(suffix)] if suffix else name
    stem_budget = max_width - len(suffix) - 1  # 1 cell for the ellipsis
    if stem_budget < 0:
        # Even "…" + suffix won't fit; show leading chars, plain-truncated.
        return name[: max_width - 1] + "…"
    head = (stem_budget + 1) // 2
    tail = stem_budget - head
    return stem[:head] + "…" + (stem[-tail:] if tail else "") + suffix


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


def _format_file_label(g: FileGroup, *, max_score: float = 0.0, name_budget: int = 0) -> Any:
    name = Path(g.path).name
    if name_budget > 0:
        name = _elide_middle_keep_suffix(name, name_budget)
    return _build_label(name, g.top_score, max_score)


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


class FNDApp(App[None]):
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
        # Sidebar panel state — always loaded from disk so user-tuned
        # collapse / expand state survives the next launch, even when
        # ``--collection`` is passed. The CLI flag overrides search
        # *scope* (which collections / sources are active), NOT the
        # *panel layout* (which sidebar containers are collapsed-to-
        # header, which collection rows are expanded). Earlier versions
        # zeroed every persisted set on the ``--collection`` branch and
        # silently dropped the user's panel layout after a single launch
        # with a flag.
        from fnd.state import load as _load_state

        saved = _load_state()
        self._collapsed_panels: set[str] = set(saved.collapsed_panels)
        self._expanded_collections: set[str] = set(saved.expanded_collections)
        # Prune unknown branch names so a renamed branch doesn't get
        # stuck "expanded" forever.
        self._expanded_filter_branches: set[str] = {
            b for b in saved.expanded_filter_branches if b in ("kinds", "date")
        }
        # Scope (collections / sources / filters) — override when
        # ``--collection`` was passed, otherwise restore the persisted
        # scope so the TUI starts where the user left it.
        if collection:
            self._collections: list[str] = [collection]
            self._active_sources: list[str] = []
            self._filter_kinds: list[str] = []
            self._filter_date: str = "any"
        else:
            self._collections = list(saved.collections)
            self._active_sources = list(saved.sources)
            self._filter_kinds = list(saved.filter_kinds)
            self._filter_date = saved.filter_date or "any"
        self._initial_query = initial_query
        self._searcher: Searcher | None = None
        self._current_query: str = ""
        # Cached match-spec for the active query — drives the markdown-
        # widget highlight subclasses, the per-line plain renderer's
        # highlight pass, and the match-aware scrollbar marker map. The
        # spec captures the SAME literal / fuzzy / synonym semantics
        # the cascade uses, so any word the searcher would have hit on
        # gets the user-visible highlight (not just exact-stem hits).
        # Recomputed on every ``_run_query``.
        self._current_match_spec: MatchSpec = MatchSpec()
        # Distraction-free reading toggle. When ``False`` the renderers
        # see an empty MatchSpec and emit no highlight spans / scrollbar
        # markers, leaving the preview as plain text. The current
        # query stays intact so flipping the toggle back on restores
        # highlights without re-running the search. Bound to ``h`` via
        # the action registry.
        self._highlights_enabled: bool = True
        # Last :multi block's intent line, if any. Disables strong-signal
        # bypass and biases snippet selection (UX-pass-4 §3). None until
        # the user submits a :multi block.
        self._current_intent: str | None = None
        self._groups: list[FileGroup] = []
        # Most-recent SearchTrace, populated on every _run_query so the
        # :explain overlay (UX-pass-4 §2) can dump it as JSON. None until
        # the first search runs.
        self._latest_trace: SearchTrace | None = None
        self._fnd_keymap = keymap or load_keymap()
        # Synonyms for §9c cascade and §9d fusion's ``syn`` sub-query.
        # Missing file → empty table → no synonym expansion (no-op).
        from fnd.config import app_data_dir
        from fnd.synonyms import SynonymTable, load_synonyms

        try:
            self._synonyms: SynonymTable = load_synonyms(app_data_dir() / "synonyms.toml")
        except Exception:
            self._synonyms = SynonymTable()
        # Ranking profile applied at search time. Built from the active
        # collection's ``ranking_profile`` field; default profile (all-zero)
        # is the BM25 identity, so the no-config case is unchanged.
        self._config = config
        # Reading mode: hide the sidebar so the preview fills the width for
        # clean text selection / distraction-free reading. Session-only;
        # owns mouse capture (off while reading so the terminal handles
        # drag-select, right-click Copy, ⌘C, macOS Speak-selection).
        self._reading_mode: bool = False
        self._ranking_profile: RankingProfile = self._resolve_profile()
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
        # Per-file flat-buffer value cache (Stage 1c). One shared
        # LineBufferPreview is mounted on first need and re-installed
        # via set_prebuilt_view for every (parent_id, query_sig)
        # activation. ``_active_flat_buffer`` is the shared widget when
        # flat is the visible preview, else None.
        self._flat_buffer_cache: OrderedDict[tuple[str, str], RenderedDocument] = OrderedDict()
        self._active_flat_buffer: LineBufferPreview | None = None
        self._shared_flat_buffer: LineBufferPreview | None = None
        # (parent_id, query_sig) of whichever RenderedDocument is currently
        # installed in the shared widget. Lets intra-file navigation skip
        # set_prebuilt_view and just scroll.
        self._installed_flat_key: tuple[str, str] | None = None
        # Async indexer state — see fnd/tui/indexer_modal.py. None when
        # no indexer is running; populated for the lifetime of one
        # `fnd collection reindex` invoked from the TUI / palette.
        self._indexer_task: asyncio.Task[None] | None = None
        self._indexer_cancel: asyncio.Event | None = None
        self._indexer_events: asyncio.Queue[Any] | None = None
        self._indexer_state: Any = None
        self._indexer_last_event: Any = None
        # Update-all-collections chain bookkeeping. The IndexerScreen
        # title shows "(N of M)" when total > 1; drive_indexer in
        # indexer_modal.py dequeues from _indexer_chain_remaining at
        # the end of each collection's run.
        self._indexer_chain_remaining: list[str] = []
        self._indexer_chain_total: int = 1
        # True between drive_indexer scheduling the next chain step via
        # call_later and the deferred task actually starting. The
        # IndexerScreen drain reads this so the modal stays mounted
        # across that gap. Without the guard the drain pops the modal
        # as soon as _indexer_chain_remaining empties (which happens
        # synchronously before call_later fires) and the next
        # collection's events have no consumer.
        self._indexer_chain_callback_pending: bool = False
        # Per-run texturise override carried across chain steps. None
        # means follow the "Texturise PDFs while indexing" toggle; True
        # forces texturising on (set by the shared "Update everything"
        # action); False forces it off (set by the "Process new files
        # index-only" action). Reset to None when the chain finishes.
        self._indexer_texturise_override: bool | None = None
        # Run-mode flags for the indexer chain, stashed so each chain step
        # inherits them. skip_unchanged=False + force_fresh=True is the
        # "Re-texturise outdated" action; defaults give incremental indexing
        # with durable cache reuse.
        self._indexer_skip_unchanged: bool = True
        self._indexer_force_fresh: bool = False
        # Per-collection final snapshots captured as each chain step
        # finishes. Drives the IndexerScreen's history band and the
        # post-chain summary screen so the user can see what every
        # finished collection produced - even after the modal moved
        # on. Cleared by the Done action on IndexerScreen.
        self._indexer_chain_history: list[Any] = []
        self._indexer_collection: str = ""
        self._indexer_started_at: str = ""
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
        self._lazy_mount_suppressed_until: float = 0.0
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
        if self._searcher is None:
            return
        # A new query must always re-render the first result, even when it
        # lands on the same (parent, seq) as the last one — release the
        # in-flight coalescing latch so this query's dispatch isn't
        # mistaken for a redundant same-tick duplicate of the previous.
        self._inflight_preview_target = None
        from fnd.filter_dsl import FilterError
        from fnd.query_dsl import split_metadata_filter

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
        # Build a comprehensive MatchSpec covering literal stems +
        # fuzzy-AUTO variants + synonym expansions, mirroring the
        # cascade's match semantics. Every preview render this query
        # drives reads from this single spec so the highlight rules
        # never drift from the search rules.
        defaults = self._config.defaults if self._config else None
        self._current_match_spec = MatchSpec.from_query(
            lexical,
            synonyms=self._synonyms,
            auto_fuzzy=defaults.fuzzy_enabled if defaults else True,
            min_term_chars=defaults.fuzzy_min_term_chars if defaults else 0,
        )
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
        cfg_defaults = self._config.defaults if self._config else None
        sections_cap = cfg_defaults.sections_per_file_max if cfg_defaults else 200
        sections_threshold = cfg_defaults.sections_score_threshold if cfg_defaults else 0.5
        try:
            self._groups = self._search_layered(
                lexical=lexical,
                filter_prefix=filter_prefix,
                limit=50,
                sections_per_file=sections_cap,
                sections_score_threshold=sections_threshold,
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
        # Bundles bake highlight spans from the previous query, so they
        # go stale at the same moment the chunk cache does.
        self._prebuilt_cache.clear()
        self._cancel_preview_mount_task()
        self._cancel_lazy_mount_task()
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
        # Highlights baked into every cached doc are stale on query change.
        self._flat_buffer_cache.clear()
        self._reset_shared_flat_buffer()
        self._chunk_widgets = {}
        self._match_targets = {}
        self._preview_parent_id = None
        self._hide_progress_bar()
        self._refresh_results_tree()
        # Defer prefetch start so the top result's user-side render gets the
        # main thread to itself for the first ~half-second. Without the
        # delay, 10 parallel prefetch mount tasks starve the auto-load.
        self.set_timer(0.5, self._prefetch_top_results, name="prefetch-defer")

    def _search_layered(
        self,
        *,
        lexical: str,
        filter_prefix: str,
        limit: int,
        sections_per_file: int,
        sections_score_threshold: float = 0.0,
        collection: str | None,
        metadata_filter: str | None,
        active_sources: list[str] | None,
    ) -> list[FileGroup]:
        """Master plan §9c + §9d wiring + UX-pass-4 §1 strong-signal regime.

        Delegates the regime decision to :func:`fnd.layered.search_layered`
        so the TUI and CLI share one entry point. ``filter_prefix`` is
        applied via :class:`_PrefixingSearcher` so fusion + cascade +
        the regime probe all see the same effective query without any
        signature changes.
        """
        if self._searcher is None or not lexical.strip():
            self._latest_trace = None
            return []
        from fnd.layered import search_layered

        searcher = (
            _PrefixingSearcher(self._searcher, prefix=filter_prefix)
            if filter_prefix
            else self._searcher
        )
        defaults = self._config.defaults if self._config else None
        groups, trace = search_layered(
            searcher,  # type: ignore[arg-type]
            query=lexical,
            limit=limit,
            sections_per_file=sections_per_file,
            sections_score_threshold=sections_score_threshold,
            collection=collection,
            synonyms=self._synonyms,
            metadata_filter=metadata_filter,
            active_sources=active_sources,
            intent=self._current_intent,
            profile=self._ranking_profile,
            auto_fuzzy_enabled=defaults.fuzzy_enabled if defaults else True,
            min_term_chars=defaults.fuzzy_min_term_chars if defaults else 0,
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
        # Cancel any debounced preview load from the previous result
        # set — its parent_id may no longer be a hit, and the new
        # cursor placement below will arm a fresh timer.
        self._cancel_pending_preview_load()
        tree = self.query_one("#results_pane", Tree)
        tree.clear()
        max_score = max((g.top_score for g in self._groups), default=0.0)
        budget = self._file_label_budget(tree)
        for i, g in enumerate(self._groups):
            file_node = tree.root.add(
                _styled_parent_label(
                    _format_file_label(g, max_score=max_score, name_budget=budget)
                ),
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
            # Park cursor on the first hit so the preview already shows the match.
            top_file = tree.root.children[0]
            if top_file.children:
                tree.cursor_line = 1
            # Dispatch explicitly — NodeHighlighted is suppressed when
            # cursor_line lands on the same index as before.
            top_group = self._groups[0]
            top_hit = top_group.hits[0] if top_group.hits else None
            self._schedule_preview_load(
                top_group.parent_id,
                top_hit.chunk_seq if top_hit else 0,
            )

    @staticmethod
    def _file_label_budget(tree: Tree[Any]) -> int:
        """Char budget for a file row's name: the visible content width
        (border + scrollbar excluded) minus the tree's 2-cell row prefix
        (toggle/guide, measured) and the 7-cell score column. 0 before layout."""
        return max(0, tree.scrollable_content_region.width - 2 - 7)

    def _relabel_file_rows(self) -> None:
        """Re-elide file-row labels in place (no tree rebuild, so the cursor
        and preview are untouched) — used on resize when the budget changes."""
        try:
            tree = self.query_one("#results_pane", Tree)
        except Exception:
            return
        budget = self._file_label_budget(tree)
        max_score = max((g.top_score for g in self._groups), default=0.0)
        for node in tree.root.children:
            data = node.data
            if isinstance(data, dict) and data.get("kind") == "file":
                node.set_label(
                    _styled_parent_label(
                        _format_file_label(data["group"], max_score=max_score, name_budget=budget)
                    )
                )

    def on_resize(self, _event: events.Resize) -> None:
        """Re-fit elided filenames to the new pane widths. Deferred to after
        layout so the panes report their settled geometry."""
        self.call_after_refresh(self._refit_after_resize)

    def _refit_after_resize(self) -> None:
        self._refresh_status()  # preview title
        self._relabel_file_rows()  # result rows

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
                from fnd.tui import _perf

                _perf.mark(
                    "click_to_display_end",
                    parent_id=parent_id,
                    path="already_active_scroll_only",
                )
                self._preview_scroll.reconcile()
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
                    silent=True,
                )
            )
            return

        self._cancel_preview_mount_task()
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
        """Lazy-mount the single hidden LineBufferPreview under #preview_pane."""
        import contextlib

        buf = self._shared_flat_buffer
        if buf is not None and buf.parent is not None:
            return buf
        pane = self.query_one("#preview_pane", VerticalScroll)
        for w in list(pane.children):
            if isinstance(w, Static) and w.id == "placeholder":
                with contextlib.suppress(Exception):
                    w.remove()
        buf = LineBufferPreview(wrap=True, show_match_markers=self._scrollbar_markers_enabled)
        buf.add_class("-hidden")
        pane.mount(buf)
        self._shared_flat_buffer = buf
        return buf

    def _install_flat_doc(
        self,
        buf: LineBufferPreview,
        doc: RenderedDocument,
        focus_chunk_seq: int,
        *,
        parent_id: str,
        context_fraction: float = 0.0,
    ) -> None:
        """Install ``doc`` into ``buf`` scrolled to the focused chunk's match."""
        focus_line = self._focus_line_for_chunk(doc.fv, focus_chunk_seq)
        buf.set_prebuilt_view(
            doc.fv,
            doc.strips,
            doc.visual_to_logical,
            doc.logical_to_visual_start,
            wrap_width=doc.wrap_width,
            base_width=doc.base_width,
            initial_focus_line=focus_line,
            context_fraction=context_fraction,
        )
        buf.parent_doc_id = parent_id  # type: ignore[attr-defined]

    def _reset_shared_flat_buffer(self) -> None:
        """Hide + clear the shared widget when the value cache is invalidated."""
        import contextlib

        self._active_flat_buffer = None
        self._installed_flat_key = None
        buf = self._shared_flat_buffer
        if buf is None:
            return
        with contextlib.suppress(Exception):
            buf.add_class("-hidden")
        with contextlib.suppress(Exception):
            buf.clear()

    @staticmethod
    def _focus_line_for_chunk(fv: FileView, chunk_id: int) -> int | None:
        """First matched line in ``chunk_id``, falling back to chunk start.
        Mirrors LineBufferPreview.scroll_to_chunk so the synchronous
        pre-paint scroll lands at the same place the deferred call did."""
        target = fv.first_hit_line_in_chunk.get(chunk_id)
        if target is None:
            rng = fv.chunk_to_range.get(chunk_id)
            if rng is not None:
                target = rng[0]
        return target

    def _build_file_view_for_chunks(self, chunks: list[FileChunk]) -> FileView:
        """Convert decoded chunks into a :class:`FileView` for the flat
        path. Reuses the same word-level match-span helper the
        structural renderer uses so highlight semantics — including
        the per-word colour (yellow for exact matches, orange for
        fuzzy ones) — agree across pipelines."""
        spec = self._effective_match_spec
        import os

        if (
            os.environ.get("_FND_FLAT_MD_STYLED") == "1"
            and chunks
            and any(c.kind == "md" and c.body_md for c in chunks)
        ):
            from fnd.tui._md_flat import build_md_file_view

            try:
                pane_widget = self.query_one("#preview_pane", VerticalScroll)
                wrap_width = max(20, pane_widget.content_size.width - 1)
            except Exception:
                wrap_width = 80
            return build_md_file_view(chunks, spec=spec, wrap_width=wrap_width)
        triples: list[tuple[int, str, list[tuple[int, int] | tuple[int, int, str]]]] = []
        for c in chunks:
            body_text = "\n".join(b.text for b in c.blocks)
            spans = _build_match_spans(body_text, spec) if not spec.is_empty else []
            styled_spans: list[tuple[int, int] | tuple[int, int, str]] = [
                (s.start, s.end, str(s.style)) for s in spans
            ]
            triples.append((c.chunk_seq, body_text, styled_spans))
        return build_file_view(triples)

    def _activate_flat_buffer(self, buf: LineBufferPreview) -> None:
        """Show ``buf`` and hide every other preview widget (structural
        containers and other flat buffers) so only one file is on
        screen at a time."""
        from fnd.tui import _perf

        self._clear_pane_placeholder()
        for child in self.query(PreviewContainer):
            child.add_class("-hidden")
        for child in self.query(LineBufferPreview):
            if child is buf:
                child.remove_class("-hidden")
            else:
                child.add_class("-hidden")
        self._active_flat_buffer = buf
        self._active_preview = None
        # Reset the structural-path alias dicts so any straggler scroll
        # call can't accidentally try to scroll to a now-orphaned widget.
        self._chunk_widgets = {}
        self._match_targets = {}
        _perf.mark(
            "click_to_display_end",
            parent_id=getattr(buf, "parent_doc_id", None),
            path="flat_activate",
        )

    def _current_query_signature(self) -> str:
        """Stable signature for the current query — match-bearing
        widgets are baked with this query's highlights, so the cache
        must invalidate when it changes. Includes intent because intent
        biases snippet selection (UX-pass-4 §3), and the highlight
        toggle state because the rendered spans differ on/off: without
        it, toggling highlights re-uses the opposite-state cached
        container for the same file + query and the toggle has no
        visible effect."""
        return (
            f"{self._current_query}|{self._current_intent or ''}|hl={int(self._highlights_enabled)}"
        )

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
        by the swap."""
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
        # Yield + refresh so Textual's compositor recomputes each
        # built widget's region — build_done fires when Markdown.update
        # returns, but the compositor's coordinate pass runs on the
        # next refresh cycle. Scrolling before that means the focus
        # chunk's region.y is still stale (often 0), the user sees
        # the correct match for one paint then jumps as layout settles.
        # On heavy md (large tables, fences) a single refresh tick
        # isn't enough — keep yielding until the focus chunk's
        # region.height is non-zero (capped so we don't hang).
        for _ in range(20):
            await asyncio.sleep(0)
            container.refresh(layout=True)
            await asyncio.sleep(0)
            if isinstance(header, FNDMarkdown) and header.region.height > 0:
                break
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

        # Scroll while the container is still invisible (opacity:0), then
        # reveal only once the scroll has committed — so the match never
        # flashes at the file top before jumping into place.
        self.call_after_refresh(self._preview_scroll.reconcile, _reveal_when_landed)
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

        # Scroll while invisible (opacity:0), reveal once it lands — see
        # _finalize_via_lock for why the order matters.
        self.call_after_refresh(self._preview_scroll.reconcile, _reveal_when_landed)

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
        silent: bool = False,
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
            # lazy-mount radius. Mounting every chunk of a 5000-chunk
            # PDF takes minutes AND inflates DOM enough to break the
            # input-lag envelope; the radius bounds both.
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
                        self._suppress_lazy_mount_briefly()
                        self._preview_scroll.reconcile()
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

    def _suppress_lazy_mount_briefly(self, duration: float = 0.4) -> None:
        """Push the gate forward so the next ``_check_preview_lazy_mount``
        runs no-op until ``duration`` seconds have passed. Used by every
        programmatic scroll site (navigation anchor, finalize reveal,
        cold-mount scroll) so their own watcher trips don't trigger a
        lazy mount that would yank scroll position away from the
        focused chunk."""
        import time

        deadline = time.monotonic() + duration
        if deadline > self._lazy_mount_suppressed_until:
            self._lazy_mount_suppressed_until = deadline

    def _check_preview_lazy_mount(self) -> None:
        """Scroll watcher entry point (after debounce). Mounts the next
        batch of chunks in the scroll direction when the viewport
        approaches a boundary of the chunk currently under it. Looks
        at the next *unmounted* chunk in document order — so gaps left
        behind when the user jumps between matches get filled
        progressively, not just the chunks past the absolute max/min
        mounted index."""
        self._lazy_mount_check_timer = None
        # While the scroll controller is armed (a navigation is still
        # settling) it owns the preview position; user-scroll-driven lazy
        # mount stays suppressed until the user takes control or the document
        # finishes mounting (both call release()). State-based, so it can't
        # expire mid-settle the way the old time gate did.
        if self._preview_scroll.is_armed:
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

        # Mount chunks [start_idx, start_idx-1, …, start_idx-batch+1]
        # in reverse so each new widget lands BEFORE the anchor in
        # document order. ``hidden`` MUST be revealed even on cancel,
        # otherwise a switch-away mid-batch leaves widgets with
        # ``display=False`` cached on the container; the next visit
        # paints those rows as blank space → the "Workshop section
        # only shows the heading" symptom. No scroll compensate —
        # anchor preservation via virtual_region delta proved
        # unreliable on consecutive above-batches (returns 0 even
        # after refresh+sleep), so newly-prepended chunks just
        # appear at the top of the visible area, which is the right
        # UX when the user just scrolled up to the wall.
        end = max(start_idx - _LAZY_MOUNT_BATCH, -1)
        hidden: list[Widget] = []
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

            for w in hidden:
                w.display = True
            hidden.clear()
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
        rendered Content. Code fences keep the stock ``MarkdownFence``
        rendering (syntax-highlighted via Rich), so query terms inside
        a code block don't muddy the syntax colours.

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

    def _scroll_preview_to_chunk(
        self,
        focus_chunk_seq: int,
        *,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        if self._active_flat_buffer is not None:
            self._active_flat_buffer.scroll_to_chunk(focus_chunk_seq, prefer_first_match=True)
            if on_done is not None:
                on_done()
            return
        header = self._chunk_widgets.get(focus_chunk_seq)
        if header is None:
            if on_done is not None:
                on_done()
            return
        for w in self._chunk_widgets.values():
            w.remove_class("chunk-section-focused")
        # Apply focused band to chunks that don't already manage their
        # own focus highlight (FNDMarkdown handles that internally).
        if not isinstance(header, FNDMarkdown):
            header.add_class("chunk-section-focused")
        self.call_after_refresh(self._do_scroll_to_chunk, focus_chunk_seq, 30, on_done)

    def _do_scroll_to_chunk(
        self,
        focus_chunk_seq: int,
        retries: int = 30,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        """Shim → StructuralScrollStrategy (removed in the dead-code cleanup task)."""
        self._preview_scroll_structural._do_scroll_to_chunk(focus_chunk_seq, retries, on_done)

    # ── StructuralHost accessors ──────────────────────────────────
    # The structural scroll strategy reads the pane, chunk/match maps,
    # match spec and lazy-mount gate back off the app through these.
    def preview_pane(self) -> VerticalScroll:
        return self.query_one("#preview_pane", VerticalScroll)

    def effective_match_spec(self) -> MatchSpec:
        return self._effective_match_spec

    def suppress_lazy_mount_briefly(self, duration: float = 0.4) -> None:
        self._suppress_lazy_mount_briefly(duration)

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

    def _do_scroll_to_widget(self, widget: Widget, retries: int = 8) -> None:
        # Retry while the widget's region is unknown — scroll_to_widget
        # returns False without scrolling when virtual_region.size is
        # empty, which is the normal state immediately after mount.
        if widget.region.height == 0 and retries > 0:
            self.call_after_refresh(self._do_scroll_to_widget, widget, retries - 1)
            return
        pane = self.query_one("#preview_pane", VerticalScroll)
        self._suppress_lazy_mount_briefly()
        pane.scroll_to_widget(widget, top=True, animate=False)

    # ── Open dispatch ─────────────────────────────────────────────

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
    ) -> bool:
        """Spawn the async indexer task for ``collection``. Idempotent —
        if a task is already running, returns False without starting a
        second one.

        ``texturise_override`` (None/True/False), ``skip_unchanged`` and
        ``force_fresh`` are forwarded through ``drive_indexer`` to
        ``run_indexer``; for chain runs they are stashed on the app so
        subsequent chain steps inherit the same mode.

        Returns True when a new task was spawned.
        """
        import datetime as _dt

        from fnd.config import load as _load_config

        if self._indexer_task is not None and not self._indexer_task.done():
            if open_modal:
                self.push_screen(IndexerScreen(self._indexer_collection or collection))
            return False
        if config is None:
            cfg = _load_config()
            config = cfg.collection(collection)
        if index_dir is None:
            # Use the app's configured index_dir so a test or CLI
            # caller that constructed FNDApp(index_dir=...) actually
            # writes to that directory. ``self._index_dir`` is set
            # from the constructor (falling back to ``default_index_dir()``
            # there) and is always non-None.
            index_dir = self._index_dir
        self._indexer_collection = collection
        self._indexer_started_at = _dt.datetime.now(tz=_dt.UTC).isoformat(timespec="seconds")
        # First step of a chain (or single-collection run) sets the
        # mode; later chain steps re-enter start_indexer via
        # _start_next_in_chain, which passes the stashed values back.
        self._indexer_texturise_override = texturise_override
        self._indexer_skip_unchanged = skip_unchanged
        self._indexer_force_fresh = force_fresh
        self._indexer_cancel = asyncio.Event()
        # Reuse the existing events queue when a chain run is in
        # progress so the IndexerScreen's drain (which holds a
        # reference to the queue from its on_mount) keeps seeing
        # events from the next collection. Otherwise the modal would
        # appear to stall after the first collection completes.
        chain_active = bool(self._indexer_chain_remaining) or (self._indexer_chain_total or 1) > 1
        if not chain_active or self._indexer_events is None:
            self._indexer_events = asyncio.Queue()
        if not chain_active:
            # Fresh chain start: clear session-wide per-page counters
            # so this run reports its own avg from scratch.
            with contextlib.suppress(Exception):
                from fnd.tui.live_progress import reset_session as _live_reset_session

                _live_reset_session()
        self._indexer_state = None
        self._indexer_last_event = None
        self._indexer_task = asyncio.create_task(
            drive_indexer(
                self,
                collection=collection,
                config=config,
                index_dir=index_dir,
                rebuild=rebuild,
                cancel=self._indexer_cancel,
                events=self._indexer_events,
                texturise_override=texturise_override,
                skip_unchanged=skip_unchanged,
                force_fresh=force_fresh,
            )
        )
        if open_modal:
            chain_total = getattr(self, "_indexer_chain_total", 1) or 1
            chain_pending = getattr(self, "_indexer_chain_remaining", None) or []
            chain_index = max(1, chain_total - len(chain_pending))
            self.push_screen(
                IndexerScreen(
                    collection,
                    chain_total=chain_total,
                    chain_index=chain_index,
                )
            )
        return True

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
        """If the pdf-structure extra is installed and the first-reindex
        warning hasn't been seen, show it; on confirm, start the
        indexer. Otherwise start the indexer directly.

        ``rebuild=True`` is for callers that need a fresh build after a
        config change (collection rename, source delete, etc.) — it
        drops the collection's existing chunks before re-indexing."""
        from fnd.config import load as _load_config
        from fnd.tui.first_reindex_warning import (
            FirstReindexWarningScreen,
            count_pdfs,
            has_been_seen,
        )

        # Prefer the in-memory config so tests / live edits don't get
        # silently overridden by whatever's on disk.
        cfg = self._config if self._config is not None else _load_config()
        if collection not in cfg.collections:
            self.notify(f"Collection '{collection}' not found.", severity="error")
            return
        col_cfg = cfg.collections[collection]

        # Only warn when extras are actually installed (otherwise the
        # cost is the old flat-extraction cost, which is sub-second/PDF).
        from fnd.extras import EXTRAS, is_extra_installed

        extra = EXTRAS.get("pdf-structure")
        show_warning = extra is not None and is_extra_installed(extra) and not has_been_seen()

        if show_warning:
            n_pdfs = count_pdfs(col_cfg)
            if n_pdfs == 0:
                # No PDFs in this collection; skip the warning entirely.
                self.start_indexer(
                    collection=collection,
                    config=col_cfg,
                    rebuild=rebuild,
                    texturise_override=texturise_override,
                    skip_unchanged=skip_unchanged,
                    force_fresh=force_fresh,
                )
                return

            def _after_warning(confirmed: bool | None) -> None:
                if confirmed:
                    self.start_indexer(
                        collection=collection,
                        config=col_cfg,
                        rebuild=rebuild,
                        texturise_override=texturise_override,
                        skip_unchanged=skip_unchanged,
                        force_fresh=force_fresh,
                    )

            self.push_screen(
                FirstReindexWarningScreen(collection=collection, n_pdfs=n_pdfs),
                _after_warning,
            )
        else:
            self.start_indexer(
                collection=collection,
                config=col_cfg,
                rebuild=rebuild,
                texturise_override=texturise_override,
                skip_unchanged=skip_unchanged,
                force_fresh=force_fresh,
            )

    def _maybe_resume_indexer(self) -> None:
        """If a state file from a previous run exists, restart the
        indexer in background mode (no modal). Called from on_mount."""
        from fnd.config import default_index_dir
        from fnd.index_runner import load_state, state_file_for

        # Resume the default collection only for now — extending to
        # named collections requires walking ~/Library/Application
        # Support/fnd/reindex/ for *.state.toml files.
        state = load_state(state_file_for("default"))
        if state is None or state.files_completed >= state.total_files:
            return
        # Auto-resume opt-out via config flag (defaults.indexer_auto_resume).
        try:
            from fnd.config import load as _load_config

            cfg = _load_config()
            if not cfg.defaults.indexer_auto_resume:
                return
        except Exception:
            pass
        try:
            self.start_indexer(
                collection="default", index_dir=default_index_dir(), open_modal=False
            )
            self.notify(
                f"Resuming indexing: {state.files_completed}/{state.total_files}",
                timeout=5,
            )
        except Exception:
            pass

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
            # Just-expanded nodes don't have line indices yet for their
            # children; defer the cursor move until the next render tick
            # so move_cursor lands on the right line.
            tree_ref = tree
            node_ref = node
            self.call_after_refresh(
                lambda: tree_ref.move_cursor(node_ref.children[0]) if node_ref.children else None
            )
            return
        first_child = node.children[0]
        tree.move_cursor(first_child)

    @property
    def _effective_match_spec(self) -> MatchSpec:
        """The MatchSpec the renderers should consult. Falls back to an
        empty spec when the user has toggled highlights off — the
        preview pane then renders the plain document with no yellow /
        orange overlays and no scrollbar match markers."""
        return self._current_match_spec if self._highlights_enabled else MatchSpec()

    def action_toggle_highlights(self) -> None:
        """Flip the search-highlight overlay on/off without re-running
        the query. Re-renders the currently-shown preview file from
        scratch so the new state takes effect immediately on whatever
        the user is reading."""
        self._highlights_enabled = not self._highlights_enabled
        self.notify(
            "Highlights " + ("on" if self._highlights_enabled else "off"),
            timeout=1.5,
        )
        self._rerender_current_preview()

    def action_toggle_fuzzy(self) -> None:
        """Flip ``defaults.fuzzy_enabled`` in the config TOML and re-run
        the current search so the new state is visible immediately.
        Per-term ``~N`` modifiers still trigger fuzzy expansion when
        the toggle is off — only the auto-fuzzy pass is gated."""
        from fnd.config import default_config_path, write_setting

        current = self._config.defaults.fuzzy_enabled if self._config else True
        new_value = not current
        try:
            self._config = write_setting(
                config_path=default_config_path(),
                dotted_path="defaults.fuzzy_enabled",
                value=new_value,
            )
        except Exception as e:
            self.notify(f"Couldn't toggle fuzzy: {e}", severity="error", timeout=3)
            return
        self.notify(
            "Fuzzy " + ("on" if new_value else "off"),
            timeout=1.5,
        )
        if self._current_query.strip():
            self._run_query(self._current_query)

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
        """Drop the current result set and preview without re-running.

        Used when the user changes scope (toggles a collection) and the
        existing results are about to go stale — but we don't want to
        steal focus or thrash through a fresh search until the user
        explicitly asks for one. Mirrors the cache invalidation in
        ``_run_query`` minus the actual search call and the
        ``tree.focus()`` step inside ``_refresh_results_tree``.
        """
        import contextlib

        self._groups = []
        self._chunk_cache.clear()
        self._prebuilt_cache.clear()
        self._cancel_preview_mount_task()
        self._cancel_lazy_mount_task()
        self._cancel_pending_preview_load()
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
        # Rebuild the results tree (now empty). The empty-groups branch
        # in ``_refresh_results_tree`` skips ``tree.focus()``, so focus
        # stays in the panel the user is currently driving.
        self._refresh_results_tree()

    def _persist_state(self) -> None:
        """Save the current scope + panel state to disk so the next
        launch starts where the user left off."""
        from fnd.state import UiState, save

        save(
            UiState(
                collections=list(self._collections),
                sources=list(self._active_sources),
                collapsed_panels=sorted(self._collapsed_panels),
                expanded_collections=sorted(self._expanded_collections),
                expanded_filter_branches=sorted(self._expanded_filter_branches),
                filter_kinds=list(self._filter_kinds),
                filter_date=self._filter_date,
            )
        )

    # ── Collections panel (UX-D) ─────────────────────────────────

    def _collection_source_ids(self, name: str) -> list[str]:
        """Resolved source IDs for a collection, in declaration order."""
        cfg = self._config
        if cfg is None:
            return []
        col = cfg.collections.get(name)
        if col is None:
            return []
        return [str(Path(str(s.path)).expanduser().resolve()) for s in col.sources]

    def _collection_marker(self, name: str) -> str:
        """Tri-state marker for the collection row: full / partial / empty.

        ``_collections`` membership is the primary "whole collection in
        scope" signal — it's set by the CLI ``--collection`` flag,
        persisted scope, and the UI toggle handler — and reads as ●
        full. The per-source ``_active_sources`` set carries the
        finer-grained on/off bits for individual rows and produces the
        ◐ partial state when only some sources are active.

        The toggle handler keeps these in sync (it removes the parent
        from ``_collections`` when a single source is turned off, and
        re-adds it when every sibling is back on), so the only paths
        that land in "collection in ``_collections`` but no sources in
        ``_active_sources``" are the CLI flag and the legacy persisted
        scope — both of which the user explicitly wants displayed as ●.
        """
        if name in self._collections:
            return "●"
        source_ids = self._collection_source_ids(name)
        if not source_ids:
            return "○"
        active_sources = set(self._active_sources)
        n_active = sum(1 for sid in source_ids if sid in active_sources)
        if n_active == 0:
            return "○"
        if n_active == len(source_ids):
            return "●"
        return "◐"

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
            from fnd.config import load as load_config

            try:
                cfg = load_config()
            except Exception:
                cfg = None
        names = sorted(cfg.collections.keys()) if cfg else []
        active_sources = set(self._active_sources)
        # Drop persisted expand entries for collections that no longer
        # exist so the saved set stays bounded over time.
        self._expanded_collections &= set(names)
        tree.show_root = False
        tree.clear()
        active_source_count = 0
        total_source_count = 0
        n_full_collections = 0
        for name in names:
            col = cfg.collections[name] if cfg else None
            marker = self._collection_marker(name)
            if marker == "●":
                n_full_collections += 1
            n_sources = len(col.sources) if col else 0
            total_source_count += n_sources
            label = f"{marker}  {name}  ({n_sources} source{'s' if n_sources != 1 else ''})"
            node = tree.root.add(
                _styled_parent_label(label),
                data={"kind": "collection", "name": name},
                expand=name in self._expanded_collections,
            )
            if col:
                # When the whole collection is in scope (CLI flag,
                # persisted scope, or "all sources on" toggle), each
                # source is implicitly active — the per-source toggle
                # only fills in granular off-bits within an explicitly
                # full collection.
                collection_full = name in self._collections
                for i, s in enumerate(col.sources):
                    source_id = str(Path(str(s.path)).expanduser().resolve())
                    src_active = collection_full or source_id in active_sources
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
        title = f"Collections · {n_full_collections}/{len(names)} active"
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
        # Branch expand state lives in ``_expanded_filter_branches`` and
        # is persisted across runs. Re-sync it from the live tree before
        # clearing so a NodeExpanded that came in between refreshes isn't
        # lost. (Pruning to known branches happens in __init__.)
        for branch in tree.root.children:
            data = branch.data if isinstance(branch.data, dict) else {}
            cat = data.get("category")
            if isinstance(cat, str) and cat in ("kinds", "date"):
                if branch.is_expanded:
                    self._expanded_filter_branches.add(cat)
                else:
                    self._expanded_filter_branches.discard(cat)
        tree.show_root = False
        tree.clear()

        active_kinds = set(self._filter_kinds)
        kind_summary = f"{len(active_kinds)} of {len(_FILTER_KINDS)}" if active_kinds else "any"
        kind_node = tree.root.add(
            _styled_parent_label(f"File type        ({kind_summary})"),
            data={"kind": "filter_category", "category": "kinds"},
            expand="kinds" in self._expanded_filter_branches,
        )
        for k in _FILTER_KINDS:
            marker = "●" if k in active_kinds else "○"
            kind_node.add_leaf(
                f"{marker}  {k}",
                data={"kind": "filter_value", "category": "kinds", "value": k},
            )

        date_summary = self._filter_date or "any"
        date_node = tree.root.add(
            _styled_parent_label(f"Modified         ({date_summary})"),
            data={"kind": "filter_category", "category": "date"},
            expand="date" in self._expanded_filter_branches,
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
        """Enter on a collection node toggles the whole collection's
        scope (all sources at once); Enter on a single source row
        toggles that source independently. Source toggles bubble up so
        the parent collection marker reads ●/◐/○ — full / partial /
        empty — depending on how many of its sources are now active.

        Both toggle paths read "currently on" from BOTH state signals
        (``_collections`` membership + per-source ``_active_sources``)
        so the visible marker drives the toggle direction, even from
        the legacy / CLI-flag entry case where ``_collections`` has the
        collection but ``_active_sources`` hasn't been populated yet.
        """
        data = ev.node.data or {}
        kind = data.get("kind")
        if kind == "collection":
            name = str(data.get("name") or "")
            if not name:
                return
            source_ids = self._collection_source_ids(name)
            # ``name in _collections`` means "whole collection in scope"
            # — either the user just toggled it via the UI (which also
            # filled ``_active_sources``) or the scope arrived from
            # ``--collection`` / legacy persisted state (no per-source
            # bits). Either way the marker reads ● and Enter should
            # turn it off.
            currently_full = name in self._collections or (
                bool(source_ids) and all(sid in self._active_sources for sid in source_ids)
            )
            if currently_full:
                if name in self._collections:
                    self._collections.remove(name)
                if source_ids:
                    keep = set(self._active_sources) - set(source_ids)
                    # Preserve the user's relative ordering of the kept
                    # sources (set difference loses it).
                    self._active_sources = [s for s in self._active_sources if s in keep]
            else:
                if name not in self._collections:
                    self._collections.append(name)
                for sid in source_ids:
                    if sid not in self._active_sources:
                        self._active_sources.append(sid)
        elif kind == "source":
            source_id = str(data.get("source_id") or "")
            if not source_id:
                return
            parent_name = str(data.get("collection") or "")
            sibling_ids = self._collection_source_ids(parent_name) if parent_name else []
            # Normalise the "_collections-only" entry case (CLI flag,
            # legacy persisted scope) before deciding the toggle: a
            # source row reads ● when the parent collection is in
            # ``_collections``, so flesh out ``_active_sources`` to
            # match before flipping a single bit. The very next branch
            # will pop the toggled source back off, leaving every
            # untouched sibling in ``_active_sources`` — the partial
            # state the user expected to land in.
            if parent_name and parent_name in self._collections and sibling_ids:
                for sid in sibling_ids:
                    if sid not in self._active_sources:
                        self._active_sources.append(sid)
            if source_id in self._active_sources:
                self._active_sources.remove(source_id)
                # Source went off — the parent collection can no longer
                # be "fully on" by the per-source rule. Drop it from
                # ``_collections`` so the search scope reflects what
                # the user sees (partial / empty marker, not a full
                # collection filter).
                if parent_name and parent_name in self._collections:
                    self._collections.remove(parent_name)
            else:
                self._active_sources.append(source_id)
                # Source went on — if that was the last off source in
                # its collection, the collection is now fully on.
                if (
                    parent_name
                    and sibling_ids
                    and all(sid in self._active_sources for sid in sibling_ids)
                    and parent_name not in self._collections
                ):
                    self._collections.append(parent_name)
        else:
            return
        self._ranking_profile = self._resolve_profile()
        # In-place marker swap on the toggled node (+ siblings whose
        # markers depend on the same source state) instead of
        # ``_refresh_collections_panel()``, which calls ``tree.clear()``
        # and resets the cursor to the root every time the user
        # toggles.
        self._update_collections_panel_node(ev.node)
        self._refresh_collections_panel_title()
        self._refresh_status()
        self._persist_state()
        # Don't auto-rerun the active query: the user may be batch-
        # toggling several collections, and each rerun would shift focus
        # to the results pane (via _refresh_results_tree.focus()) and
        # interrupt the run. Drop the now-stale results so it's obvious
        # the next Enter in the query bar re-runs against the new scope;
        # keep _current_query so the user's last query is recallable in
        # the input.
        if self._current_query and self._groups:
            self._clear_query_results()

    def _update_collections_panel_node(self, node: Any) -> None:
        """Swap the marker on a toggled node + cascade to dependent rows.

        Preserves cursor (no ``tree.clear()`` involved). When a
        collection row is toggled, every source child marker is
        repainted too. When a source row is toggled, the parent
        collection's marker is recomputed so its tri-state (●/◐/○)
        reads the new source state.
        """
        data = node.data if isinstance(node.data, dict) else {}
        kind = data.get("kind")
        if kind == "collection":
            name = str(data.get("name") or "")
            if not name:
                return
            self._repaint_collection_node(node, name)
            for child in node.children:
                self._repaint_source_node(child)
            return
        if kind == "source":
            self._repaint_source_node(node)
            parent = node.parent
            if parent is None:
                return
            parent_data = parent.data if isinstance(parent.data, dict) else {}
            parent_name = str(parent_data.get("name") or "")
            if parent_name:
                self._repaint_collection_node(parent, parent_name)

    def _repaint_collection_node(self, node: Any, name: str) -> None:
        cfg = self._config
        col = cfg.collections.get(name) if cfg else None
        n_sources = len(col.sources) if col else 0
        marker = self._collection_marker(name)
        label = f"{marker}  {name}  ({n_sources} source{'s' if n_sources != 1 else ''})"
        node.set_label(_styled_parent_label(label))

    def _repaint_source_node(self, node: Any) -> None:
        data = node.data if isinstance(node.data, dict) else {}
        source_id = str(data.get("source_id") or "")
        if not source_id:
            return
        parent_name = str(data.get("collection") or "")
        # Mirror the rule used in ``_refresh_collections_panel``: when
        # the parent collection is in ``_collections`` (CLI / persisted /
        # toggled-on whole-collection), every child source reads as ●
        # even if ``_active_sources`` is empty.
        collection_full = bool(parent_name) and parent_name in self._collections
        src_marker = "●" if collection_full or source_id in self._active_sources else "○"
        current_label = str(node.label)
        # The source label is "<marker>  <i>. <short>" — preserve the
        # ordinal and basename, just swap the marker glyph.
        if len(current_label) > 1 and current_label[0] in ("●", "○"):
            node.set_label(src_marker + current_label[1:])
        else:
            node.set_label(current_label)

    def _refresh_collections_panel_title(self) -> None:
        """Recompute the panel's border-title counts after a toggle.

        Pulled out so toggle handlers can update the counts without
        going through the cursor-resetting tree rebuild. The "active"
        collection count tracks rows that paint as ``●`` (full) — the
        same per-source rule the row marker uses — so the title and
        the row glyphs always agree.
        """
        try:
            tree = self.query_one("#collections_panel_tree", Tree)
        except Exception:
            return
        cfg = self._config
        names = sorted(cfg.collections.keys()) if cfg else []
        active_sources = set(self._active_sources)
        total_source_count = sum(len(cfg.collections[n].sources) for n in names if cfg)
        active_source_count = 0
        n_full_collections = 0
        for n in names:
            if self._collection_marker(n) == "●":
                n_full_collections += 1
            col = cfg.collections[n] if cfg else None
            if not col:
                continue
            for s in col.sources:
                source_id = str(Path(str(s.path)).expanduser().resolve())
                if source_id in active_sources:
                    active_source_count += 1
        title = f"Collections · {n_full_collections}/{len(names)} active"
        if total_source_count and active_source_count:
            title += f", {active_source_count}/{total_source_count} sources"
        tree.border_title = title

    @on(Tree.NodeExpanded, "#collections_panel_tree")
    def _on_collection_branch_expanded(self, ev: Tree.NodeExpanded[dict[str, object]]) -> None:
        data = ev.node.data if isinstance(ev.node.data, dict) else {}
        if data.get("kind") != "collection":
            return
        name = str(data.get("name") or "")
        if name and name not in self._expanded_collections:
            self._expanded_collections.add(name)
            self._persist_state()

    @on(Tree.NodeCollapsed, "#collections_panel_tree")
    def _on_collection_branch_collapsed(self, ev: Tree.NodeCollapsed[dict[str, object]]) -> None:
        data = ev.node.data if isinstance(ev.node.data, dict) else {}
        if data.get("kind") != "collection":
            return
        name = str(data.get("name") or "")
        if name and name in self._expanded_collections:
            self._expanded_collections.discard(name)
            self._persist_state()

    @on(Tree.NodeExpanded, "#filters_panel_tree")
    def _on_filter_branch_expanded(self, ev: Tree.NodeExpanded[dict[str, object]]) -> None:
        data = ev.node.data if isinstance(ev.node.data, dict) else {}
        if data.get("kind") != "filter_category":
            return
        cat = str(data.get("category") or "")
        if cat in ("kinds", "date") and cat not in self._expanded_filter_branches:
            self._expanded_filter_branches.add(cat)
            self._persist_state()

    @on(Tree.NodeCollapsed, "#filters_panel_tree")
    def _on_filter_branch_collapsed(self, ev: Tree.NodeCollapsed[dict[str, object]]) -> None:
        data = ev.node.data if isinstance(ev.node.data, dict) else {}
        if data.get("kind") != "filter_category":
            return
        cat = str(data.get("category") or "")
        if cat in self._expanded_filter_branches:
            self._expanded_filter_branches.discard(cat)
            self._persist_state()

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
        """Worker that drops + rebuilds chunks for ``name``. Notifies on
        start/finish/error. Reused by SourceFormScreen, RenameCollection,
        and the Reindex action in the per-collection sub-menu."""
        # Reload config so we hit the latest source list.
        import contextlib

        from fnd.config import load
        from fnd.index import build_index_from_config

        with contextlib.suppress(Exception):
            self._config = load()
        cfg = self._config
        if cfg is None or name not in cfg.collections:
            return
        col = cfg.collections[name]
        index_dir = self._index_dir

        def _run() -> None:
            self.call_from_thread(
                self.notify,
                f"Reindexing {name}…",
                severity="information",
                timeout=3,
            )
            try:
                n = build_index_from_config(
                    config=col, collection=name, index_dir=index_dir, rebuild=True
                )
            except Exception as e:
                self.call_from_thread(self.notify, f"Reindex failed: {e}", severity="error")
                return
            self.call_from_thread(self._on_reindex_complete)
            self.call_from_thread(
                self.notify,
                f"Indexed {n} chunks for {name}.",
                severity="information",
            )

        self.run_worker(_run, thread=True, exclusive=True, group=f"reindex-{name}")

    def _on_reindex_complete(self) -> None:
        """Swap the in-memory ``Searcher`` for a fresh one after a rebuild.

        The captured ``self._index.searcher()`` inside ``Searcher`` reads
        from the index generation it was opened against; once the writer
        commits new chunks, the old searcher still returns hits from the
        previous generation. Rebuilding the ``Searcher`` is cheap (just
        reopens the directory) and the in-flight ``_chunk_cache`` is
        invalidated by ``_run_query`` immediately below so callers don't
        see ghost rows from the old gen.
        """
        try:
            self._searcher = Searcher(index_dir=self._index_dir)
        except (FileNotFoundError, RuntimeError):
            self._searcher = None
        if self._current_query:
            self._run_query(self._current_query)

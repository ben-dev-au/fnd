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
from typing import Any, ClassVar

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.content import Span
from textual.widget import Widget
from textual.widgets import (
    Input,
    Markdown,
    ProgressBar,
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
    MarkdownTD,
    MarkdownTH,
    MarkdownUnorderedListItem,
)
from textual.widgets.tree import TreeNode

from acorn import opener
from acorn.config import Config, default_index_dir
from acorn.explain import SearchTrace
from acorn.matching import MatchSpec
from acorn.query import FileChunk, FileGroup, Hit, Searcher
from acorn.render import (
    render_chunk_pieces,
    word_highlight_runs,
)
from acorn.rerank import RankingProfile, profile_from_config
from acorn.tui.actions import REGISTRY, Keymap, load_keymap
from acorn.tui.line_buffer import FileView, LineBufferPreview, build_file_view
from acorn.tui.preview_dispatcher import choose_preview_mode
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
# Cache *every* complete file (1+ chunks) so revisits are O(1) and old
# containers get LRU-evicted+removed from the DOM. The previous threshold
# of 30 meant short markdown files never made it to the cache, never got
# evicted, and stacked up in the preview pane — every file switch then
# paid an O(N) walk in ``_activate_preview_container`` where N = files
# visited this session.
_PREVIEW_CACHE_MIN_CHUNKS = 1
# Visible-first mount window — chunks are decoded already, mounting
# focused ± these counts synchronously gives the user instant viewport
# feedback before the background fill starts.
_VISIBLE_FIRST_ABOVE = 7
_VISIBLE_FIRST_BELOW = 7
# Lazy-mount budget. The background fill stops at focused ± this many
# chunks instead of mounting the whole document. For a 5000-chunk PDF
# that turns a multi-minute mount into a bounded one (and keeps the
# steady-state DOM small enough that post-load navigation stays
# snappy). If the user wants a section outside the buffer, they click
# it in the results tree — ``_dispatch_preview_mount`` resumes the
# task with a new focus, Phase 1a mounts the requested chunk, and
# Phase 2 extends the buffer around it.
_BACKGROUND_FILL_RADIUS = 200


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
        self.chunk_widgets: dict[int, Widget] = {}
        # chunk_seq → first match-bearing widget (or header when no match).
        self.match_targets: dict[int, Widget] = {}

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
# (acorn/render.py:46) so the highlight semantics agree with snippet
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
    renderer (``acorn.render.word_highlight_runs``) so the visual
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
    document, register it on the parent ``AcornMarkdown`` so the
    preview pane can scroll to it. First-write-wins: subsequent matched
    blocks don't overwrite.
    """
    if not spans:
        return
    md = block._markdown  # weakref unwrap
    if isinstance(md, AcornMarkdown) and md._first_match_block is None:
        md._first_match_block = block


def _apply_highlights_after_build(block: MarkdownBlock) -> None:
    """Common ``build_from_token`` postlude shared by every highlight-
    aware subclass: pull ``match_spec`` off the parent AcornMarkdown,
    compute spans against ``block._content.plain``, and replace the
    block's content with the span-augmented version. No-op when the
    parent isn't an AcornMarkdown (e.g. the stock Markdown widget the
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


class AcornMarkdownH1(_HighlightingBlockMixin, MarkdownH1):
    pass


class AcornMarkdownH2(_HighlightingBlockMixin, MarkdownH2):
    pass


class AcornMarkdownH3(_HighlightingBlockMixin, MarkdownH3):
    pass


class AcornMarkdownH4(_HighlightingBlockMixin, MarkdownH4):
    pass


class AcornMarkdownH5(_HighlightingBlockMixin, MarkdownH5):
    pass


class AcornMarkdownH6(_HighlightingBlockMixin, MarkdownH6):
    pass


class AcornMarkdownParagraph(_HighlightingBlockMixin, MarkdownParagraph):
    pass


class AcornMarkdownBlockQuote(_HighlightingBlockMixin, MarkdownBlockQuote):
    pass


class AcornMarkdownOrderedListItem(_HighlightingBlockMixin, MarkdownOrderedListItem):
    pass


class AcornMarkdownUnorderedListItem(_HighlightingBlockMixin, MarkdownUnorderedListItem):
    pass


class AcornMarkdownTH(_HighlightingBlockMixin, MarkdownTH):
    pass


class AcornMarkdownTD(_HighlightingBlockMixin, MarkdownTD):
    pass


class AcornMarkdown(Markdown):
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
    AcornMarkdown {
        height: auto;
    }
    """

    BLOCKS: dict[str, type[MarkdownBlock]] = {  # noqa: RUF012
        **Markdown.BLOCKS,
        "h1": AcornMarkdownH1,
        "h2": AcornMarkdownH2,
        "h3": AcornMarkdownH3,
        "h4": AcornMarkdownH4,
        "h5": AcornMarkdownH5,
        "h6": AcornMarkdownH6,
        "paragraph_open": AcornMarkdownParagraph,
        "blockquote_open": AcornMarkdownBlockQuote,
        "list_item_ordered_open": AcornMarkdownOrderedListItem,
        "list_item_unordered_open": AcornMarkdownUnorderedListItem,
        "th_open": AcornMarkdownTH,
        "td_open": AcornMarkdownTD,
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
        super().__init__(markdown=markdown, name=name, id=id, classes=classes)
        self.match_spec: MatchSpec = match_spec or MatchSpec()
        self._first_match_block: MarkdownBlock | None = None

    @property
    def first_match_block(self) -> MarkdownBlock | None:
        """The first highlighted block in document order, or ``None``
        when the source has no matches. Set by the highlight-aware
        block subclasses during ``build_from_token``."""
        return self._first_match_block


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


# Formats whose extractor produces a ``body_md`` markdown source
# suitable for the AcornMarkdown structural renderer. Other formats
# (pdf, txt) stay on the per-line plain renderer that targets
# specific matched lines for scroll precision.
_MARKDOWN_RENDERED_KINDS: frozenset[str] = frozenset({"md", "docx", "pptx"})


def _uses_markdown_renderer(c: FileChunk) -> bool:
    """True when this chunk should mount through ``AcornMarkdown``.
    A chunk needs both a markdown-capable kind AND non-empty
    ``body_md``; the empty-source fallback (defensive — schema-version
    refusal should make it unreachable) keeps stale-index loads from
    crashing the renderer."""
    return c.kind in _MARKDOWN_RENDERED_KINDS and bool(c.body_md)


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
    /* Textual's stock MarkdownFence pins scrollbar-size-horizontal /
       vertical and gives its inner Label a ``padding: 1 2`` block,
       which leaves a dark backdrop row above the scrollbar that
       reads as part of the bar — making the bottom of the fence
       look noticeably thicker than the rest of the app's hairline
       scrollbars. App-level CSS outranks widget DEFAULT_CSS, so
       force scrollbar size to 1 AND drop the bottom padding so the
       bar sits flush against the last code line. */
    MarkdownFence {
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
    }
    MarkdownFence > Label {
        padding: 0 1;
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
            self._expanded_collections: set[str] = set()
            self._expanded_filter_branches: set[str] = set()
        else:
            from acorn.state import load as _load_state

            saved = _load_state()
            self._collections = list(saved.collections)
            self._collapsed_panels = set(saved.collapsed_panels)
            self._active_sources = list(saved.sources)
            self._filter_kinds = list(saved.filter_kinds)
            self._filter_date = saved.filter_date or "any"
            self._expanded_collections = set(saved.expanded_collections)
            # Prune unknown branch names so a renamed branch doesn't get
            # stuck "expanded" forever.
            self._expanded_filter_branches = {
                b for b in saved.expanded_filter_branches if b in ("kinds", "date")
            }
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
        # Phase 5 flat-buffer pipeline: PDF / TXT files render through
        # one :class:`LineBufferPreview` widget per file instead of a
        # per-chunk widget tree. ``_flat_buffer_cache`` is an LRU keyed
        # the same way as :class:`PreviewCache` so cache-hit semantics
        # are identical. ``_active_flat_buffer`` mirrors
        # ``_active_preview`` for the flat path.
        self._flat_buffer_cache: OrderedDict[tuple[str, str], LineBufferPreview] = OrderedDict()
        self._active_flat_buffer: LineBufferPreview | None = None
        # Convenience aliases that point into the active container —
        # legacy code paths (_scroll_preview_to_chunk, etc.) read from
        # these instead of poking at the container directly.
        # Widgets here may be either per-line ``Static``s (PDF / TXT
        # plain renderer) or whole-chunk ``AcornMarkdown`` widgets (md
        # / docx / pptx structural renderer). The dict is widened to
        # ``Widget`` so both can be stored without complaint.
        self._chunk_widgets: dict[int, Widget] = {}
        self._match_targets: dict[int, Widget] = {}
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
        self._refresh_collections_panel()
        # Filters panel — kind/date selectors that compose into the query.
        ftree = self.query_one("#filters_panel_tree", Tree)
        ftree.show_root = False
        ftree.guide_depth = 2
        # Filters parents (File type / Modified) are no-ops on Enter — skip
        # past them when expanded.
        ftree._skip_expanded_parents = True  # type: ignore[attr-defined]
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
        # When the CLI handed us a query that produced results,
        # ``_refresh_results_tree`` already focused the results pane and
        # parked the cursor on the top hit; the user's first keypress
        # should advance through results instead of appending characters
        # to the search bar.
        if not self._initial_query or not self._groups:
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
            ("Spc", "Peek"),
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

        import contextlib

        with contextlib.suppress(Exception):
            self.query_one("#footer_hints", Static).update(
                render_hint_bar(self._FOOTER_ANCHORS, contextual)
            )

    def on_descendant_focus(self) -> None:  # Textual fires this on focus changes
        self._refresh_footer_hints()
        # ``ResultsTree.validate_cursor_line`` keeps the cursor off
        # expanded parents at the moment of any cursor_line change, so
        # no on-focus bounce is needed.

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
        # Build a comprehensive MatchSpec covering literal stems +
        # fuzzy-AUTO variants + synonym expansions, mirroring the
        # cascade's match semantics. Every preview render this query
        # drives reads from this single spec so the highlight rules
        # never drift from the search rules.
        self._current_match_spec = MatchSpec.from_query(
            lexical, synonyms=self._synonyms, fuzzy=True
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
        # Same lifecycle for the flat-buffer cache: highlights were
        # baked from the previous query, so every cached widget is
        # stale. Drop all of them from the DOM.
        for buf in list(self._flat_buffer_cache.values()):
            with contextlib.suppress(Exception):
                buf.remove()
        self._flat_buffer_cache.clear()
        if self._active_flat_buffer is not None and self._active_flat_buffer.parent is not None:
            with contextlib.suppress(Exception):
                self._active_flat_buffer.remove()
        self._active_flat_buffer = None
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
        sections_score_threshold: float = 0.0,
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
            sections_score_threshold=sections_score_threshold,
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
                _styled_parent_label(_format_file_label(g, max_score=max_score)),
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
            # Drop the cursor onto the first hit of the auto-expanded top
            # file — the preview already renders that hit, so leaving the
            # cursor on the parent file row would force a redundant Down
            # keypress before navigation actually advances to a new match.
            # ``cursor_line = 1`` lands on line 1 (root is hidden, line 0
            # is the top file, line 1 is its first child) without needing
            # the per-node line index that ``move_cursor`` relies on —
            # which isn't built until the next render tick.
            top_file = tree.root.children[0]
            if top_file.children:
                tree.cursor_line = 1

    # ── Preview ───────────────────────────────────────────────────

    @on(Tree.NodeHighlighted)
    def _on_tree_highlight(self, ev: Tree.NodeHighlighted[Any]) -> None:
        # ``ResultsTree.validate_cursor_line`` already keeps the cursor
        # off expanded parents, so by the time NodeHighlighted fires the
        # cursor is guaranteed to be on a selectable row. No bounce
        # needed here — just dispatch the preview render.
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
        # Hide the previously-active container so the user sees a clean
        # "loading" state during the decode. Without this, large files
        # (e.g. a 1000-page PDF with thousands of chunks) keep showing
        # the previous file's content for the full decode wall-clock,
        # making the click look unresponsive.
        if self._active_preview is not None and self._active_preview.parent_doc_id != parent_id:
            self._active_preview.add_class("-hidden")
        self._show_progress_bar(total=None)

        target_parent_id = parent_id
        target_focus = focus_chunk_seq
        searcher = self._searcher
        # Pull the worker count from config so users can tune the
        # decode parallelism via Settings without code edits. 1 = serial.
        decode_workers = (
            self._config.defaults.preview_decode_workers if self._config is not None else 1
        )
        app = self

        def _load() -> None:
            try:
                fetched = searcher.get_file_chunks(target_parent_id, max_workers=decode_workers)
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

        # Phase 5 redesign: route by format. PDF / TXT take the flat-
        # buffer path (one widget per file, line API, line-precise
        # scrollbar markers). MD / DOCX / PPTX stay on the structural
        # Markdown widget below.
        if choose_preview_mode(chunks) == "flat":
            self._dispatch_flat_buffer_mount(parent_id, focus_chunk_seq, chunks)
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
        # Sweep any PreviewContainer in the preview pane that isn't tracked
        # by the LRU cache. This catches containers whose mount was
        # cancelled before completion (rapid file switching) — those
        # never reached ``_preview_cache.put`` and would otherwise
        # accumulate, slowing every subsequent ``_activate_preview_container``
        # walk in proportion to the leak count.
        import contextlib as _contextlib

        cached_containers = set(self._preview_cache._cache.values())
        for stranded in list(self.query(PreviewContainer)):
            if stranded not in cached_containers:
                with _contextlib.suppress(Exception):
                    stranded.remove()
        if self._active_preview is not None and self._active_preview not in cached_containers:
            self._active_preview = None
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

    def _dispatch_flat_buffer_mount(
        self,
        parent_id: str,
        focus_chunk_seq: int,
        chunks: list[FileChunk],
    ) -> None:
        """Flat-buffer mount path — one :class:`LineBufferPreview`
        widget per file. No multi-phase mount, no per-chunk widget
        tree, no progressive load — once the chunks are decoded the
        FileView builds in one pass and the widget paints in one
        pass too.

        Cache hits flip ``-hidden`` on the previously-cached widget
        and scroll to the focus; cold loads build a fresh FileView,
        mount a new widget, and install the view.
        """
        import contextlib

        query_sig = self._current_query_signature()
        cache_key = (parent_id, query_sig)

        # Cache hit: flip visible, scroll, hide bar. Match markers are
        # already baked into the cached buffer's scrollbar so no extra
        # refresh is needed.
        cached = self._flat_buffer_cache.get(cache_key)
        if cached is not None and cached.parent is not None:
            self._flat_buffer_cache.move_to_end(cache_key)
            self._activate_flat_buffer(cached)
            cached.scroll_to_chunk(focus_chunk_seq, prefer_first_match=True)
            self._hide_progress_bar()
            self._preview_parent_id = parent_id
            self._refresh_status()
            return

        # Cold path: build the FileView from decoded chunks, mount a
        # fresh LineBufferPreview.
        pane = self.query_one("#preview_pane", VerticalScroll)
        # Drop the placeholder if it's still mounted.
        for w in list(pane.children):
            if isinstance(w, Static) and "placeholder" in w.classes:
                with contextlib.suppress(Exception):
                    w.remove()

        fv = self._build_file_view_for_chunks(chunks)
        buf = LineBufferPreview(wrap=True)
        pane.mount(buf)
        self._activate_flat_buffer(buf)
        buf.set_file_view(fv)
        buf.scroll_to_chunk(focus_chunk_seq, prefer_first_match=True)

        # LRU-cache the buffer so revisits within the same query are
        # instant (display flip only).
        self._flat_buffer_cache[cache_key] = buf
        self._flat_buffer_cache.move_to_end(cache_key)
        while len(self._flat_buffer_cache) > _PREVIEW_CACHE_MAX_FILES:
            _, evicted = self._flat_buffer_cache.popitem(last=False)
            with contextlib.suppress(Exception):
                evicted.remove()

        self._hide_progress_bar()
        self._preview_parent_id = parent_id
        self._refresh_status()

    def _build_file_view_for_chunks(self, chunks: list[FileChunk]) -> FileView:
        """Convert decoded chunks into a :class:`FileView` for the flat
        path. Reuses the same word-level match-span helper the
        structural renderer uses so highlight semantics agree."""
        spec = self._effective_match_spec
        triples: list[tuple[int, str, list[tuple[int, int]]]] = []
        for c in chunks:
            body_text = "\n".join(b.text for b in c.blocks)
            spans = _build_match_spans(body_text, spec) if not spec.is_empty else []
            byte_spans = [(s.start, s.end) for s in spans]
            triples.append((c.chunk_seq, body_text, byte_spans))
        return build_file_view(triples)

    def _activate_flat_buffer(self, buf: LineBufferPreview) -> None:
        """Show ``buf`` and hide every other preview widget (structural
        containers and other flat buffers) so only one file is on
        screen at a time."""
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
            # Phase 1a: mount the focused chunk first and yield so it
            # paints before the surrounding context mounts. On large
            # files the rest of the visible window can take several
            # hundred ms to mount; the user clicked a specific match
            # and should see THAT chunk's content first, not stare at a
            # progress bar while neighbouring chunks slowly fill in.
            if focus_idx not in container.mounted_indices:
                self._mount_chunk_into(container, chunks[focus_idx], focus_idx, chunks)
            self._scroll_preview_to_chunk(focus_chunk_seq)
            self._update_progress_bar(progress=len(container.mounted_indices))
            await asyncio.sleep(0)

            # Phase 1b: mount the rest of the visible window. Closest-to-
            # focus first, alternating below/above, so chunks adjacent to
            # the user's eye fill in before the edges of the viewport.
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
            # PDF takes minutes AND keeps the post-load DOM big enough
            # to make navigation laggy; the radius bounds both costs.
            below_end = min(len(chunks), focus_idx + 1 + _BACKGROUND_FILL_RADIUS)
            for i in range(win_end, below_end):
                if i in container.mounted_indices:
                    continue
                self._mount_chunk_into(container, chunks[i], i, chunks)
                self._update_progress_bar(progress=len(container.mounted_indices))
                # Yield every chunk so a slow mount can't peg the UI
                # thread between yields. asyncio.sleep(0) hands control
                # back to the loop so pending key/redraw events run.
                await asyncio.sleep(0)
            await asyncio.sleep(0)

            # Phase 2b: hidden-prepend ABOVE the window, capped at the
            # same radius. Each newly-mounted widget gets ``display =
            # False`` immediately, so it takes no layout space and the
            # focused chunk doesn't drift while the rest mounts.
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
            # Cache the container even when the mount didn't run to
            # completion. For monster files (1000+ page PDFs with
            # thousands of chunks) the user reliably navigates away
            # before is_complete becomes True; without caching the
            # partial container, every revisit re-mounts from scratch
            # and the file looks like it has no cache. The resume path
            # in ``_dispatch_preview_mount`` skips already-mounted
            # indices so partial-cache hits paint the previously-
            # mounted region instantly and continue the fill in the
            # background.
            evicted = self._preview_cache.put(container)
            for old in evicted:
                with contextlib.suppress(Exception):
                    old.remove()
            if container.is_complete:
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
            # Partial mounts leave the bar visible — a revisit will
            # resume from ``mounted_indices`` and the bar reflects
            # progress.
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

    def _refresh_match_scrollbar(self, chunks: list[FileChunk]) -> None:
        """Build a per-chunk match map and forward it to the preview's
        custom scrollbar so chunk-match positions are visible on the bar."""
        from acorn.render import text_has_any_match

        try:
            pane = self.query_one("#preview_pane", MatchAwareScroll)
        except Exception:
            return
        spec = self._effective_match_spec
        match_map = [any(text_has_any_match(b.text, spec) for b in c.blocks) for c in chunks]
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

    def _mount_structured_chunk(
        self,
        parent: Container | VerticalScroll,
        c: FileChunk,
        *,
        before: Widget | None = None,
    ) -> None:
        """Structural markdown rendering for formats whose extractor
        populated ``body_md`` (md / docx / pptx).

        Mounts a single :class:`AcornMarkdown` widget per chunk —
        Textual builds out the per-block widget tree (headings,
        paragraphs, tables, fenced code, lists, blockquotes) and our
        highlight-aware subclasses overlay match-only spans on the
        rendered Content. Code fences keep the stock ``MarkdownFence``
        rendering (syntax-highlighted via Rich), so query terms inside
        a code block don't muddy the syntax colours.

        ``_chunk_widgets`` maps the chunk seq to the AcornMarkdown
        widget itself (used for chunk-boundary scrolling); ``_match_
        targets`` maps to ``first_match_block`` when the chunk has
        matches, falling back to the AcornMarkdown so scroll still
        lands at the chunk top when nothing matched.
        """
        source = c.body_md or _legacy_blocks_to_md(c.blocks)
        md_widget = AcornMarkdown(
            source,
            match_spec=self._effective_match_spec,
            classes="chunk-section chunk-md-body chunk-first",
        )
        parent.mount(md_widget, before=before)
        self._chunk_widgets[c.chunk_seq] = md_widget
        # ``first_match_block`` is populated by the highlight-aware
        # subclasses during build_from_token, which fires after mount
        # but before the user can interact, so this is set by the
        # time the next scroll-to-match request lands.
        self._match_targets[c.chunk_seq] = md_widget

    def _scroll_preview_to_chunk(self, focus_chunk_seq: int) -> None:
        # Flat-buffer path: the widget owns its own scroll and knows
        # how to land on the matched line within a chunk.
        if self._active_flat_buffer is not None:
            self._active_flat_buffer.scroll_to_chunk(focus_chunk_seq, prefer_first_match=True)
            return
        header = self._chunk_widgets.get(focus_chunk_seq)
        target = self._match_targets.get(focus_chunk_seq) or header
        if target is None:
            return
        # Mark the focused chunk's header so single-line plain chunks
        # (PDF / TXT) get a subtle accent band on the matched line.
        # Skip the marker on AcornMarkdown widgets because the tinted
        # background reads as an "ugly brown" overlay across the whole
        # rendered chunk — for markdown chunks the per-word search
        # highlight is already the visual indicator.
        for w in self._chunk_widgets.values():
            w.remove_class("chunk-section-focused")
        if header is not None and not isinstance(header, AcornMarkdown):
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
        - Collapsed branch with children → expand it AND drop the cursor
          onto its first child (the preview already shows that child, so
          leaving the cursor on the parent file row would force a wasted
          Down keypress before a fresh match comes into view).
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
        self._cancel_preview_mount_task()
        evicted = self._preview_cache.clear()
        for old in evicted:
            with contextlib.suppress(Exception):
                old.remove()
        if self._active_preview is not None and self._active_preview.parent is not None:
            with contextlib.suppress(Exception):
                self._active_preview.remove()
        self._active_preview = None
        for buf in list(self._flat_buffer_cache.values()):
            with contextlib.suppress(Exception):
                buf.remove()
        self._flat_buffer_cache.clear()
        if self._active_flat_buffer is not None and self._active_flat_buffer.parent is not None:
            with contextlib.suppress(Exception):
                self._active_flat_buffer.remove()
        self._active_flat_buffer = None
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

    def _persist_state(self) -> None:
        """Save the current scope + panel state to disk so the next
        launch starts where the user left off."""
        from acorn.state import UiState, save

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
        # Drop persisted expand entries for collections that no longer
        # exist so the saved set stays bounded over time.
        self._expanded_collections &= set(names)
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
            node = tree.root.add(
                _styled_parent_label(label),
                data={"kind": "collection", "name": name},
                expand=name in self._expanded_collections,
            )
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
        # In-place marker swap on the toggled node instead of
        # _refresh_collections_panel() — that calls tree.clear() which
        # resets the cursor to the root every time the user toggles.
        self._update_collections_panel_node(ev.node)
        self._refresh_collections_panel_title()
        self._refresh_status()
        self._persist_state()
        if self._current_query:
            self._run_query(self._current_query)

    def _update_collections_panel_node(self, node: Any) -> None:
        """Swap the marker on a single collection or source node label
        without rebuilding the tree. Preserves the cursor on the
        just-toggled row."""
        data = node.data if isinstance(node.data, dict) else {}
        kind = data.get("kind")
        if kind == "collection":
            name = str(data.get("name") or "")
            if not name:
                return
            cfg = self._config
            col = cfg.collections.get(name) if cfg else None
            n_sources = len(col.sources) if col else 0
            marker = "●" if name in self._collections else "○"
            label = f"{marker}  {name}  ({n_sources} source{'s' if n_sources != 1 else ''})"
            node.set_label(_styled_parent_label(label))
            return
        if kind == "source":
            source_id = str(data.get("source_id") or "")
            if not source_id:
                return
            src_marker = "●" if source_id in self._active_sources else "○"
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
        going through the cursor-resetting tree rebuild."""
        try:
            tree = self.query_one("#collections_panel_tree", Tree)
        except Exception:
            return
        cfg = self._config
        names = sorted(cfg.collections.keys()) if cfg else []
        active_collections = set(self._collections)
        active_sources = set(self._active_sources)
        total_source_count = sum(len(cfg.collections[n].sources) for n in names if cfg)
        active_source_count = 0
        for n in names:
            col = cfg.collections[n] if cfg else None
            if not col:
                continue
            for s in col.sources:
                source_id = str(Path(str(s.path)).expanduser().resolve())
                if source_id in active_sources:
                    active_source_count += 1
        title = f"Collections — {len(active_collections)}/{len(names)} active"
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

        # Step 2 — cascade focus toward the results pane.
        ctx = self._focus_context()
        if ctx in ("query", "preview", "filters", "collections"):
            import contextlib

            with contextlib.suppress(Exception):
                self.query_one("#results_pane", Tree).focus()

    def action_show_help(self) -> None:
        """Open the Settings menu pre-navigated to the Keybindings section.

        The standalone help overlay was removed in the Settings overhaul —
        ``?`` now lands the user inside the menu's filterable Keybindings
        list, which doubles as the up-to-date cheat sheet.
        """
        from acorn.tui.menu import SECTION_KEYBINDINGS
        from acorn.tui.settings_screen import (
            SettingsScreen,
            open_settings_section,
        )

        # Already in the menu — close the current stack before pushing
        # Keybindings so Esc returns to the main app in one press.
        if isinstance(self.screen, SettingsScreen):
            self._close_settings_stack()
        open_settings_section(self, SECTION_KEYBINDINGS)

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
        from acorn.tui.settings_screen import SettingsScreen, open_settings

        if isinstance(self.screen, SettingsScreen):
            self._close_settings_stack()
            return
        open_settings(self)

    def _close_settings_stack(self) -> None:
        """Pop every nested SettingsScreen so the user returns to the
        main app. Used by the Esc cascade and by re-pressing ``:`` while
        the menu is open."""
        from acorn.tui.settings_screen import SettingsScreen

        while isinstance(self.screen, SettingsScreen):
            self.pop_screen()

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
        """Palette entry: push the Collections sub-screen directly. One
        Esc returns to the main app."""
        from acorn.tui.menu import SECTION_COLLECTIONS
        from acorn.tui.settings_screen import SettingsScreen, open_settings_section

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

        from acorn.config import (
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
        from acorn.tui.settings_screen import SettingsScreen

        while isinstance(self.screen, SettingsScreen):
            self.pop_screen()
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
        with self.suspend():
            subprocess.call([editor, str(path)])
        try:
            self._config = load()
        except Exception as e:
            from acorn.tui.config_recovery_screen import (
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

        from acorn.config import default_config_path

        path = default_config_path().parent / "keybindings.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(
                '# Acorn user keybinding overrides.\n# [normal]\n# "j"    = "focus_results_pane"\n',
                encoding="utf-8",
            )
        # Close any settings screens so the editor takes over the terminal
        # cleanly; otherwise Textual's screen_stack restoration can flash a
        # half-painted menu over the freshly-loaded TUI.
        from acorn.tui.settings_screen import SettingsScreen

        while isinstance(self.screen, SettingsScreen):
            self.pop_screen()
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
        with self.suspend():
            subprocess.call([editor, str(path)])
        # Reload the keymap so new bindings take effect immediately.
        from acorn.tui.actions import load_keymap

        self._acorn_keymap = load_keymap()
        self.notify("Reloaded keybindings", timeout=2)

    def _on_recovery_done(self, result: object) -> None:
        from acorn.config import load

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

        from acorn.config import load
        from acorn.index import build_index_from_config

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

"""Highlight-aware Markdown widget tree for the preview pane."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from typing import TYPE_CHECKING, Any

from textual import events
from textual.containers import VerticalScroll
from textual.content import Content, Span
from textual.widgets import Markdown
from textual.widgets._markdown import (
    MarkdownBlock,
    MarkdownBlockQuote,
    MarkdownFence,
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

from fnd.matching import MatchSpec, phrase_char_spans
from fnd.render import (
    DIM_STYLES,
    HIGHLIGHT_STYLE,
    match_word_spans_multi,
    phrase_gap_spans,
)
from fnd.tui.mermaid_render import MermaidRenderer
from fnd.tui.syntax_theme import highlight_fenced, inline_code_spans
from fnd.tui.widgets.callouts import rewrite_callouts
from fnd.tui.widgets.md_inline import apply_obsidian_inline

if TYPE_CHECKING:
    from rich.text import Text

__all__ = [
    "FNDMarkdown",
    "FNDMarkdownBlockQuote",
    "FNDMarkdownFence",
    "FNDMarkdownH1",
    "FNDMarkdownH2",
    "FNDMarkdownH3",
    "FNDMarkdownH4",
    "FNDMarkdownH5",
    "FNDMarkdownH6",
    "FNDMarkdownOrderedListItem",
    "FNDMarkdownParagraph",
    "FNDMarkdownTD",
    "FNDMarkdownTH",
    "FNDMarkdownTableDT",
    "FNDMarkdownUnorderedListItem",
    "_HeadingMarkerMixin",
    "_build_match_spans",
    "_compute_table_col_widths",
    "_find_first_match_coord_in_table",
    "_find_match_coords_in_table",
    "_legacy_blocks_to_md",
    "_record_first_match",
]

# ── Highlight-aware Markdown widget tree ──────────────────────────────
#
# Textual's stock Markdown widget renders a per-block widget tree out
# of markdown-it tokens (headings, paragraphs, lists, blockquotes,
# tables, fenced code, etc.). We subclass the block kinds whose inline
# text should carry search-term highlights and overlay
# ``Content.add_spans`` after the base build runs — match-only spans
# layered on top of whatever style the base block produced. Code
# fences (``MarkdownFence``) carry the overlay too (``FNDMarkdownFence``):
# the match highlight reads over the lexer's syntax colours so a query
# term inside a code block is as findable as one in prose.
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
    insertions). Proximity-group terms outside a qualifying window are
    dimmed. Same shared run helper as every other surface
    (``fnd.render.match_word_spans``) so the visual treatment is
    identical across markdown / docx / pptx / pdf / txt previews. Span
    styles are concrete Rich style strings so the visual doesn't depend
    on Textual's component-class CSS resolution.
    """
    if spec.is_empty or not plain:
        return []
    return _build_match_spans_multi((plain,), spec)[0]


def _build_match_spans_multi(plains: Sequence[str], spec: MatchSpec) -> list[list[Span]]:
    """Per-segment highlight spans with the proximity window computed across the
    WHOLE sequence — see :func:`fnd.render.match_word_spans_multi`. Segments are
    the consecutive blocks of one chunk."""
    if spec.is_empty:
        return [[] for _ in plains]
    out: list[list[Span]] = []
    for plain, runs in zip(plains, match_word_spans_multi(plains, spec), strict=True):
        spans: list[Span] = []
        covered: set[int] = set()
        for a, b, style in runs:
            spans.append(Span(a, b, style))
            covered.update(range(a, b))
        # Phrase highlighting (quoted phrase, or a stopword between content words)
        # fills only the GAPS between term spans — never overlaps them. Textual's
        # Content drops overlapping differently-styled spans, so an overlapping
        # phrase span in multi-colour mode would blank the whole word. Phrases stay
        # block-local: a quoted phrase is contiguous by definition.
        for start, end in phrase_gap_spans(phrase_char_spans(plain, spec), covered):
            spans.append(Span(start, end, HIGHLIGHT_STYLE))
        out.append(spans)
    return out


def _spans_have_full_match(spans: list[Span]) -> bool:
    """True if any span carries a non-dimmed match style — i.e. a real
    (in-window) proximity hit, phrase, or plain match, as opposed to a
    proximity-dimmed out-of-window stray."""
    return any(str(s.style) not in DIM_STYLES for s in spans)


def _append_match_block(md: FNDMarkdown, block: MarkdownBlock, *, full: bool) -> None:
    """Record a match-bearing block on ``md``: set the first-slot per tier
    (first-write-wins) and append to the ordered nav list. First-write-wins
    keeps ``first_match_block`` on the earliest full match; the ordered list
    gives match-nav every stop. Dedup-guarded so a theme-driven fence rebuild
    doesn't double-register."""
    if full:
        if md._first_match_block is None:
            md._first_match_block = block
        if block not in md._match_blocks:
            md._match_blocks.append(block)
    else:
        if md._first_dim_match_block is None:
            md._first_dim_match_block = block
        if block not in md._dim_match_blocks:
            md._dim_match_blocks.append(block)


def _record_first_match(block: MarkdownBlock, spans: list[Span]) -> None:
    """Register ``block`` as a match stop on the parent ``FNDMarkdown`` (see
    :func:`_append_match_block`). ``first_match_block`` prefers the full slot,
    so a ``{N}``/``"a b"~N`` query lands on the real co-occurrence, not an
    earlier lone-term hit (mirrors the flat path)."""
    if not spans:
        return
    md = block._markdown  # weakref unwrap
    if not isinstance(md, FNDMarkdown):
        return
    _append_match_block(md, block, full=_spans_have_full_match(spans))


def _apply_highlights_after_build(block: MarkdownBlock) -> None:
    """``build_from_token`` postlude for a block whose parent is NOT an
    ``FNDMarkdown`` — the stock Markdown widget the help overlay uses. Blocks
    under an FNDMarkdown are highlighted chunk-wide instead, by
    :func:`apply_chunk_highlights`, so a proximity window can straddle them."""
    md = block._markdown
    if isinstance(md, FNDMarkdown):
        return
    spec = getattr(md, "match_spec", None)
    if spec is None or spec.is_empty:
        return
    spans = _build_match_spans(block._content.plain, spec)
    if not spans:
        return
    block.set_content(block._content.add_spans(spans))
    _record_first_match(block, spans)


def _block_plain(block: MarkdownBlock) -> str:
    """A block's own rendered text. Fences carry theirs on ``_highlighted_code``
    (the syntax-coloured code, or the mermaid art we deliberately match against
    instead of the diagram source); every other block carries it on ``_content``.
    Container blocks — lists, table wrappers — own no text of their own."""
    code = getattr(block, "_highlighted_code", None)
    if code is not None:
        return str(code.plain)
    return str(block._content.plain)


def _content_blocks(blocks: Iterable[MarkdownBlock]) -> Iterator[MarkdownBlock]:
    """Depth-first, document order, yielding only blocks that own text.

    Walks ``_blocks`` rather than the DOM because a table's ``MarkdownTH`` /
    ``MarkdownTD`` cells are consumed into a DataTable by
    ``FNDMarkdownTableDT.compose`` and are never mounted — a post-mount query
    would miss exactly the cells where a cross-boundary window is most common.
    No block has both own text and children, so nothing is counted twice."""
    for block in blocks:
        children = getattr(block, "_blocks", None)
        if children:
            yield from _content_blocks(children)
        elif _block_plain(block):
            yield block


def _set_block_spans(block: MarkdownBlock, spans: list[Span]) -> None:
    """Apply ``spans`` to ``block`` and cache them on it.

    The cache is what survives a fence's ``notify_style_update``: that rebuilds
    ``_highlighted_code`` from scratch and drops every span, so without it the
    fence would silently fall back to recomputing block-locally — the very scope
    this pass exists to widen."""
    block._fnd_match_spans = spans  # type: ignore[attr-defined]
    if not spans:
        return
    code = getattr(block, "_highlighted_code", None)
    if code is not None:
        block._highlighted_code = code.add_spans(spans)  # type: ignore[attr-defined]
        block.set_content(block._highlighted_code)  # type: ignore[attr-defined]
    else:
        block.set_content(block._content.add_spans(spans))
    _record_first_match(block, spans)


def apply_chunk_highlights(md: FNDMarkdown, blocks: list[MarkdownBlock]) -> None:
    """Bake match highlights across a whole chunk in ONE two-tier decision.

    A proximity window is scoped to the chunk — the unit the index matched the
    slop-phrase in — not to a single block. Highlighting block-by-block made the
    token positions restart at every paragraph and list item, so a genuine
    ``{N}`` co-occurrence split across two blocks could never qualify and both
    terms rendered dimmed at any slop.

    Runs pre-mount, off the parsed block tree, so ``set_content`` costs no
    layout and the table cells are still reachable."""
    spec = getattr(md, "match_spec", None)
    if spec is None or spec.is_empty:
        return
    targets = list(_content_blocks(blocks))
    if not targets:
        return
    for block, spans in zip(
        targets, _build_match_spans_multi([_block_plain(b) for b in targets], spec), strict=True
    ):
        _set_block_spans(block, spans)


def _apply_inline_code_highlights(block: MarkdownBlock) -> None:
    """Syntax-colour inline code (`` `x` ``) the same way fenced blocks are
    coloured. The base build marks inline code with the ``.code_inline``
    style; we tokenise each such run and overlay per-token colours on top
    (foreground only — the inline-code background, if any, shows through).
    Runs before the match overlay so search terms still read over it."""
    content = block._content
    syntax_spans: list[Span] = []
    for span in content.spans:
        if str(span.style) != ".code_inline":
            continue
        text = content.plain[span.start : span.end]
        syntax_spans.extend(inline_code_spans(text, offset=span.start))
    if syntax_spans:
        block.set_content(content.add_spans(syntax_spans))


class _HighlightingBlockMixin:
    """Drop-in mixin for the MarkdownBlock subclasses that should apply
    search-term highlights after the base build. Avoids repeating the
    same five-line ``build_from_token`` body on every subclass."""

    def build_from_token(self, token):  # type: ignore[override]
        super().build_from_token(token)  # type: ignore[misc]
        _apply_inline_code_highlights(self)  # type: ignore[arg-type]
        md = self._markdown  # type: ignore[attr-defined]
        apply_obsidian_inline(self, getattr(md, "match_spec", None) or MatchSpec())  # type: ignore[arg-type]
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


class _CalloutTitleMixin:
    """Prefix a callout title with its icon and the open-fold marker."""

    def build_from_token(self, token):  # type: ignore[override]
        super().build_from_token(token)  # type: ignore[misc]
        meta = self._token.meta.get("fnd_callout_title")  # type: ignore[attr-defined]
        if meta is None:
            return
        _key, icon, foldable = meta
        prefix = f"{'▾ ' if foldable else ''}{icon}  "
        self.set_content(Content.assemble(prefix, self._content))  # type: ignore[attr-defined]


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


class FNDMarkdownParagraph(_HighlightingBlockMixin, _CalloutTitleMixin, MarkdownParagraph):
    def __init__(self, markdown: Markdown, token: Any, *args: Any, **kwargs: Any) -> None:
        super().__init__(markdown, token, *args, **kwargs)
        if "fnd_callout_title" in token.meta:
            self.add_class("callout-title")


class FNDMarkdownBlockQuote(_HighlightingBlockMixin, MarkdownBlockQuote):
    def __init__(self, markdown: Markdown, token: Any, *args: Any, **kwargs: Any) -> None:
        super().__init__(markdown, token, *args, **kwargs)
        key = token.meta.get("fnd_callout")
        if key:
            self.add_class("callout", f"callout-{key}")


class FNDMarkdownOrderedListItem(_HighlightingBlockMixin, MarkdownOrderedListItem):
    pass


class FNDMarkdownUnorderedListItem(_HighlightingBlockMixin, MarkdownUnorderedListItem):
    pass


class FNDMarkdownTH(_HighlightingBlockMixin, MarkdownTH):
    pass


class FNDMarkdownTD(_HighlightingBlockMixin, MarkdownTD):
    pass


_MERMAID_RENDERER = MermaidRenderer()


def _fence_language(token: Any) -> str:
    """First word of the fence info string, lowercased (the language)."""
    return (getattr(token, "info", "") or "").strip().split(" ", 1)[0].lower()


class FNDMarkdownFence(MarkdownFence):
    """Code fence that overlays search-term highlights on the syntax
    colouring. Stock ``MarkdownFence`` builds a syntax-highlighted
    ``Content`` in ``__init__`` and renders it via a single Label, with
    no hook for match spans — so query terms inside a code block went
    unhighlighted. We add the match overlay on top of the lexer colours
    (the highlight reads over them) right after the base build, and
    re-apply it when a theme change rebuilds the syntax colouring.

    A ``mermaid`` fence is rendered as a terminal text-art diagram instead
    (when the ``render_mermaid`` flag rides the parent markdown), falling
    back to the syntax-highlighted source on any unsupported diagram. A
    diagram wider than the pane keeps its width and gains a thin horizontal
    scrollbar (the ``mermaid-diagram`` class lifts the stock fence's
    ``overflow-x: hidden``)."""

    @classmethod
    def highlight(cls, code: str, language: str, ansi: bool = False, dark: bool = False) -> Content:
        # Stock highlighting maps Pygments tokens through a sparse theme;
        # swap in the granular FND palette. Native-ANSI terminals keep the
        # base ANSI themes (safety net — the app runs truecolor by default).
        if ansi:
            return super().highlight(code, language, ansi=ansi, dark=dark)
        return highlight_fenced(code, language or None)

    def __init__(self, markdown: Markdown, token: Any, code: str) -> None:
        super().__init__(markdown, token, code)
        # Highlights are baked chunk-wide after the whole block tree is parsed
        # (``apply_chunk_highlights``) so a proximity window can span the fence
        # and its neighbours; nothing to do per-fence here.
        self._try_render_mermaid(token, code)

    def _try_render_mermaid(self, token: Any, code: str) -> bool:
        self._mermaid_code: str | None = None
        if not getattr(self._markdown, "render_mermaid", False):
            return False
        if _fence_language(token) != "mermaid":
            return False
        art = _MERMAID_RENDERER.render(code)
        if art is None:
            return False
        self._mermaid_code = code
        self.add_class("mermaid-diagram")
        # The art lands on ``_highlighted_code`` before the chunk pass runs, so
        # matches overlay the *art*, not the fence source. The art is derived
        # from the source, so a term in a node label survives into it and
        # highlights like any other text; a term living only in mermaid syntax
        # (arrows, node ids) does not, and must not register as a match stop —
        # n/b and the ▲/▼ counts would then point at a diagram with nothing
        # visibly highlighted.
        self._set_diagram_content(art)
        return True

    def _set_diagram_content(self, art: Text) -> None:
        # termaid hands back a Rich ``Text``; the fence renders a Textual
        # ``Content`` (``set_content``/``add_spans``), so convert across.
        content = Content.from_rich_text(art)
        self._highlighted_code = content
        self.set_content(content)

    def notify_style_update(self) -> None:
        # The base rebuilds ``_highlighted_code`` from scratch on a theme
        # change, dropping our content — re-render the diagram if this is a
        # mermaid fence, else re-apply the match overlay.
        super().notify_style_update()
        code = getattr(self, "_mermaid_code", None)
        if code is not None:
            art = _MERMAID_RENDERER.render(code)
            if art is not None:
                self._set_diagram_content(art)
                self._reapply_cached_highlights()
                return
            # Re-render failed — this is no longer a diagram: drop the
            # diagram-only styling (hscroll/no-wrap) before falling back. The
            # cached spans were computed against the ART, so they address text
            # that no longer exists; drop them rather than replay them onto the
            # fence source at meaningless offsets.
            self.remove_class("mermaid-diagram")
            self._mermaid_code = None
            self._fnd_match_spans = []
        self._reapply_cached_highlights()

    def _reapply_cached_highlights(self) -> None:
        """Re-add the chunk-scoped spans the base class just threw away.

        ``notify_style_update`` rebuilds ``_highlighted_code`` from scratch on
        every theme change (and fires several times during the initial mount),
        dropping all spans. Recomputing here would use the fence's own text
        alone, undoing the chunk-wide proximity scope — so replay the cache
        instead. The length check is a backstop against a stale cache; the one
        case that actually invalidates it (a failed mermaid re-render) clears
        the cache at source."""
        spans = getattr(self, "_fnd_match_spans", None)
        if not spans:
            return
        if max(s.end for s in spans) > len(self._highlighted_code.plain):
            return
        self._highlighted_code = self._highlighted_code.add_spans(spans)
        self.set_content(self._highlighted_code)


class FNDMarkdownTableDT(MarkdownTable):
    """Render a markdown table as a single DataTable widget instead of
    ~N MarkdownTableCellContents widgets.

    This is the LIVE DEFAULT: ``FNDMarkdown.BLOCKS["table_open"]`` maps
    here, and ``compose`` only falls back to the stock per-cell grid when
    ``_FND_NO_W3=1`` (an opt-OUT, set nowhere by default). The DataTable
    path mounts ~4x fewer widgets and reflows ~2x faster on a width change
    (e.g. toggling Reading View) than the stock grid. (The old
    ``_FND_W3_DATATABLE`` opt-in flag is gone — it is read nowhere.)

    Styling matches Textual's MarkdownTable as closely as DataTable
    allows: rounded outer border + zebra rows + primary-coloured bold
    header. The inter-cell keylines that grid-layout MarkdownTableContent
    gets aren't reachable on DataTable — it renders cells as a single
    render output, not as keyline-eligible child widgets.
    """

    DEFAULT_CSS = """
    FNDMarkdownTableDT {
        & > DataTable {
            /* Textual's DataTable defaults to ``max-height: 100%`` — one
               CONTAINER height — so a table with more rows than fit in the
               chunk becomes a nested scroll region with its own scrollbar:
               scrolling the document into it scrolls the table instead, until
               it bottoms out. No content is unreachable, but the table is a
               window onto itself rather than part of the document.

               Lifting the cap lays it out at full height, so the document
               scrolls past it as it does everything else. Measured on a real
               81-row table: 44 rows on screen with a scrollbar -> all 81, none.

               Load-bearing beyond tidiness: a nested scroll region cannot be
               flattened, so a table capped this way is the one thing that
               cannot be captured as a flat run of Strips.

               An explicit ceiling rather than a keyword: Textual's scalar
               system has no ``none`` and rejects ``auto`` here, and both
               ``100%`` and ``100h`` resolve against the PARENT container
               (``vh`` is the viewport unit), so neither lifts the cap. */
            max-height: 99999;
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
        from textual.coordinate import Coordinate
        from textual.widgets import DataTable

        headers, rows = self._get_headers_and_rows()
        self._headers = headers
        self._rows = rows
        header_texts = [_content_to_text(h) for h in headers]
        row_texts = [[_content_to_text(c) for c in row] for row in rows if row]
        dt: DataTable[Any] = DataTable(cursor_type="none", zebra_stripes=True)
        # Stored so on_resize can recompute column widths when the pane
        # widens (e.g. toggling Reading View) without re-parsing the table.
        self._header_texts = header_texts
        self._row_texts = row_texts
        self._dt = dt
        # Compute per-column widths from the pane's content size so wide
        # cells wrap rather than overflow. Without this, DataTable's
        # auto_width measures each column at its longest single line —
        # paragraph cells produce ~700-cell columns that get truncated
        # to the pane's 91 cells, with no wrap.
        avail = self._available_table_width()
        self._last_avail = avail
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
        # Tiers come from the cells' cached chunk-scoped spans, so a window
        # spanning two cells of a row counts as full — see
        # ``_match_coords_from_blocks``. Falls back to the spec-derived scan for
        # a non-FNDMarkdown parent, which never ran the chunk pass.
        if isinstance(md, FNDMarkdown):
            matches = _match_coords_from_blocks(self)
        else:
            spec = getattr(md, "match_spec", None) or MatchSpec()
            matches = _find_match_coords_in_table(headers, rows, spec)
        # Full-match cells (dim proximity strays skipped) so match-nav hops
        # only between genuine hits; the first match stays the scroll target.
        dt._fnd_match_coords = [Coordinate(*rc) for rc, full in matches if full]  # type: ignore[attr-defined]
        if matches:
            match_coord, is_full = matches[0]
            dt._fnd_match_coord = Coordinate(*match_coord)  # type: ignore[attr-defined]
            # Register self as parent's match block — TH/TD widgets are
            # bypassed so _record_first_match never fires. The table is one
            # stop in match_blocks; nav expands it to its matching cells via
            # _fnd_match_coords.
            if isinstance(md, FNDMarkdown):
                _append_match_block(md, self, full=is_full)
        yield dt

    def _available_table_width(self) -> int:
        """Width budget for the table's columns at the current pane size.

        The block is ``width: 1fr`` so its own ``content_size`` tracks the
        pane once mounted; before mount it isn't sized, so fall back to the
        pane query and finally the app width minus the sidebar budget. ``-3``
        is the scrollbar (1) + DataTable outer border (2)."""
        own = self.content_size.width
        if own > 3:
            return own - 3
        try:
            pane = self.app.query_one("#preview_pane", VerticalScroll)
            avail = max(0, pane.content_size.width - 3)
        except Exception:
            avail = 0
        if avail <= 0:
            avail = max(40, self.app.size.width - 52)
        return avail

    def on_resize(self, _event: events.Resize) -> None:
        """Recompute column widths when the block's width changes — e.g.
        toggling Reading View hides the sidebar and widens the pane. The
        DataTable's columns are sized once at ``compose``; without this they
        stay at the old width and the table reads compressed in the wider
        pane (cells wrapping that no longer need to)."""
        dt = getattr(self, "_dt", None)
        if dt is None or not dt.columns:
            return
        avail = self._available_table_width()
        if avail <= 0 or avail == getattr(self, "_last_avail", None):
            return
        self._last_avail = avail
        col_widths = _compute_table_col_widths(
            self._header_texts, self._row_texts, available_width=avail, cell_padding=dt.cell_padding
        )
        if not col_widths or len(col_widths) != len(dt.columns):
            return
        for column, w in zip(dt.columns.values(), col_widths, strict=True):
            column.auto_width = False
            column.width = w
        # ``_require_update_dimensions`` is a reactive flag: assigning True
        # schedules DataTable's own recompute of the virtual size from the
        # new column widths on the next refresh.
        dt._require_update_dimensions = True
        dt.refresh()


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


def _find_match_coords_in_table(
    headers: list[Any], rows: list[list[Any]], spec: MatchSpec
) -> list[tuple[tuple[int, int], bool]]:
    """Every matching cell as ``((row, col), is_full)``, full matches first.

    A cell matches iff ``text_has_any_match`` does — a word match OR a
    quoted-phrase span, the same gate the highlight overlay applies — so each
    coordinate points at a cell that is actually highlighted (quoted phrases
    included).
    (Checking the Content's ``spans`` instead is wrong: that set also
    carries the markdown styling spans — inline code, emphasis, links —
    so a merely *styled* cell would slip in over a *matched* one.)
    ``text_has_any_match`` short-circuits on the first matching word and
    skips the per-char alignment / Span allocation that building the full
    highlight spans here would waste on every cell of a large table.

    For a proximity query (``{N}``/``NEAR/N``/``"a b"~N``) a cell can match
    only via a dimmed out-of-window stray; full (in-window) cells sort ahead
    of dim-only ones so the scroll target and first nav stop land on the
    genuine co-occurrence, mirroring the flat and block paths. Plain queries
    never dim, so every hit is full.

    Header hits map to row 0 col c as a best-effort approximation since the
    DataTable cursor doesn't address headers directly.
    """
    from fnd.render import text_has_any_match, text_has_full_match

    if spec.is_empty:
        return []
    prox = bool(spec.proximity_groups)

    def _tier(plain: str) -> bool | None:
        """``None`` no match · ``True`` full (qualifying) · ``False`` dim-only."""
        if not text_has_any_match(plain, spec):
            return None
        return (not prox) or text_has_full_match(plain, spec)

    return _merge_cell_tiers(
        [_tier(getattr(h, "plain", "") or "") for h in headers],
        [[_tier(getattr(c, "plain", "") or "") for c in row] for row in rows],
    )


def _merge_cell_tiers(
    header_tiers: list[bool | None], row_tiers: list[list[bool | None]]
) -> list[tuple[tuple[int, int], bool]]:
    """Fold per-cell tiers (``None`` no match · ``True`` full · ``False`` dim-only)
    into ``((row, col), is_full)`` coordinates, full matches first.

    Header hits and a first-data-row hit both map to ``(0, col)`` (the header has
    no cursor coordinate of its own), so a match in both would emit the
    coordinate twice — inflating the count and making n/b land on it twice.
    Merge by coordinate, keeping the strongest tier (full beats dim-only) and
    first-seen order (headers before rows)."""
    tier_by_coord: dict[tuple[int, int], bool] = {}
    order: list[tuple[int, int]] = []

    def _add(coord: tuple[int, int], tier: bool | None) -> None:
        if tier is None:
            return
        if coord not in tier_by_coord:
            order.append(coord)
        tier_by_coord[coord] = tier_by_coord.get(coord, False) or bool(tier)

    for col, tier in enumerate(header_tiers):
        _add((0, col), tier)
    for r_idx, row in enumerate(row_tiers):
        for c_idx, tier in enumerate(row):
            _add((r_idx, c_idx), tier)
    out = [(coord, tier_by_coord[coord]) for coord in order]
    # Full matches first (stable within tier) so the single scroll target and
    # the first nav stop prefer a real co-occurrence over a dim-only stray.
    out.sort(key=lambda t: not t[1])
    return out


def _match_coords_from_blocks(table: Any) -> list[tuple[tuple[int, int], bool]]:
    """Table match coordinates derived from the cells' CACHED chunk-scoped spans.

    The spec-derived :func:`_find_match_coords_in_table` re-tests each cell's
    text on its own, which reinstates the per-block window the chunk pass exists
    to widen — a ``{N}`` co-occurrence split across two cells of one row would
    sort as dim-only. Reading the cached spans keeps the tier consistent with
    what was actually painted."""
    from textual.widgets._markdown import MarkdownTD, MarkdownTH, MarkdownTR

    def _tier(block: MarkdownBlock) -> bool | None:
        spans = getattr(block, "_fnd_match_spans", None)
        if not spans:
            return None
        return _spans_have_full_match(spans)

    header_tiers: list[bool | None] = []
    row_tiers: list[list[bool | None]] = []
    for block in _flatten_blocks(table):
        if isinstance(block, MarkdownTH):
            header_tiers.append(_tier(block))
        elif isinstance(block, MarkdownTR):
            row_tiers.append([])
        # A TD before any TR means a table with no header row — not reachable
        # through GFM, which requires the delimiter row, but an unguarded
        # ``row_tiers[-1]`` here would take the whole preview down.
        elif isinstance(block, MarkdownTD) and row_tiers:
            row_tiers[-1].append(_tier(block))
    if row_tiers and not row_tiers[-1]:
        row_tiers.pop()
    return _merge_cell_tiers(header_tiers, row_tiers)


def _flatten_blocks(block: MarkdownBlock) -> Iterator[MarkdownBlock]:
    """Depth-first walk of ``_blocks``, mirroring the order Textual's
    ``MarkdownTable._get_headers_and_rows`` uses so cell coordinates agree.

    Children are yielded BEFORE their parent, so a row's cells arrive before its
    ``MarkdownTR``. That lag is load-bearing, not incidental: the header row's TR
    is what opens row 0, so the first body row's TDs land in it and every
    subsequent TR opens the next row. Switching to pre-order would shift every
    data row down by one and leave row 0 empty."""
    for child in block._blocks:
        if child._blocks:
            yield from _flatten_blocks(child)
        yield child


def _find_first_match_coord_in_table(
    headers: list[Any], rows: list[list[Any]], spec: MatchSpec
) -> tuple[tuple[int, int], bool] | None:
    """First matching cell (full preferred), or ``None``. Thin wrapper over
    :func:`_find_match_coords_in_table` kept for the single-target callers."""
    matches = _find_match_coords_in_table(headers, rows, spec)
    return matches[0] if matches else None


class FNDMarkdown(Markdown):
    """Markdown widget with inline search-term highlighting.

    Subclasses ``textual.widgets.Markdown`` and registers
    highlight-aware block subclasses for the kinds whose inline text
    should carry the highlight overlay (headings, paragraphs,
    blockquotes, list items, table cells, fenced code). Fenced code
    blocks use ``FNDMarkdownFence``, which overlays match spans on the
    syntax-highlighted Content so in-code matches are visible too.

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
    /* Callouts: typed left bar plus a 12% tint of the same colour. ``outer``
       is a half-block glyph — the only border style that tiles solid in every
       terminal font we tested, SF Mono included. */
    FNDMarkdown .callout { border-left: outer $primary; background: $primary 12%; }
    FNDMarkdown .callout > .callout-title { color: $primary; text-style: bold; }
    FNDMarkdown .callout-note { border-left: outer $accent; background: $accent 12%; }
    FNDMarkdown .callout-note > .callout-title { color: $accent; }
    FNDMarkdown .callout-info { border-left: outer $accent; background: $accent 12%; }
    FNDMarkdown .callout-info > .callout-title { color: $accent; }
    FNDMarkdown .callout-todo { border-left: outer $accent; background: $accent 12%; }
    FNDMarkdown .callout-todo > .callout-title { color: $accent; }
    FNDMarkdown .callout-abstract { border-left: outer $primary; background: $primary 12%; }
    FNDMarkdown .callout-abstract > .callout-title { color: $primary; }
    FNDMarkdown .callout-tip { border-left: outer $success; background: $success 12%; }
    FNDMarkdown .callout-tip > .callout-title { color: $success; }
    FNDMarkdown .callout-success { border-left: outer $success; background: $success 12%; }
    FNDMarkdown .callout-success > .callout-title { color: $success; }
    FNDMarkdown .callout-question { border-left: outer $secondary; background: $secondary 12%; }
    FNDMarkdown .callout-question > .callout-title { color: $secondary; }
    FNDMarkdown .callout-warning { border-left: outer $warning; background: $warning 12%; }
    FNDMarkdown .callout-warning > .callout-title { color: $warning; }
    FNDMarkdown .callout-failure { border-left: outer $error; background: $error 12%; }
    FNDMarkdown .callout-failure > .callout-title { color: $error; }
    FNDMarkdown .callout-danger { border-left: outer $error; background: $error 12%; }
    FNDMarkdown .callout-danger > .callout-title { color: $error; }
    FNDMarkdown .callout-bug { border-left: outer $error; background: $error 12%; }
    FNDMarkdown .callout-bug > .callout-title { color: $error; }
    FNDMarkdown .callout-example { border-left: outer $primary; background: $primary 12%; }
    FNDMarkdown .callout-example > .callout-title { color: $primary; }
    /* A neutral wash, not $boost: that resolves fully transparent in
       tokyo-night, leaving quote callouts with no fill at all. */
    FNDMarkdown .callout-quote { border-left: outer $foreground 50%; background: $foreground 12%; }
    FNDMarkdown .callout-quote > .callout-title { color: $foreground 70%; }
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
        "fence": FNDMarkdownFence,
        "code_block": FNDMarkdownFence,
    }

    def __init__(
        self,
        markdown: str | None = None,
        *,
        match_spec: MatchSpec | None = None,
        render_mermaid: bool = False,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        import asyncio as _asyncio

        super().__init__(markdown=markdown, name=name, id=id, classes=classes)
        self.match_spec: MatchSpec = match_spec or MatchSpec()
        # Read by ``FNDMarkdownFence`` to decide whether a mermaid fence
        # renders as a diagram (default-on flag, off in tests unless set).
        self.render_mermaid: bool = render_mermaid
        # ``_first_match_block`` holds the first FULL (qualifying) match block;
        # ``_first_dim_match_block`` the first block whose only matches are dimmed
        # proximity strays. ``first_match_block`` prefers the full slot so a
        # ``{N}``/``"a b"~N`` query scrolls to the real co-occurrence.
        self._first_match_block: MarkdownBlock | None = None
        self._first_dim_match_block: MarkdownBlock | None = None
        # Ordered match stops for intra-file nav: full-match blocks in build
        # (document) order, dim-only ones collected separately as a fallback.
        self._match_blocks: list[MarkdownBlock] = []
        self._dim_match_blocks: list[MarkdownBlock] = []
        # Set by ``_on_mount`` after ``super()._on_mount`` (which awaits
        # ``Markdown.update``) returns. Lets the scroll path event-trigger
        # on build completion instead of polling.
        self.build_done: _asyncio.Event = _asyncio.Event()
        # Bumped per ``update()``; the build-completion callback only sets
        # ``build_done`` when its generation is still current, so a
        # superseded render's future (which fires its done-callback on
        # completion *or* cancellation) can't wake waiters early.
        self._build_gen: int = 0

    @property
    def first_match_block(self) -> MarkdownBlock | None:
        """The block the preview scrolls to, or ``None`` when the source
        has no matches. Prefers the first FULL (qualifying) match; only when
        no full match exists anywhere does it fall back to the first dimmed
        proximity stray. Set by the highlight-aware block subclasses during
        ``build_from_token`` (and the table block during ``compose``)."""
        return self._first_match_block or self._first_dim_match_block

    @property
    def match_blocks(self) -> list[MarkdownBlock]:
        """Ordered match stops of this chunk for intra-file nav: the full-match
        blocks in document order, or the dim-only ones when no full match
        exists anywhere in the chunk (mirrors ``first_match_block``'s tiering)."""
        return self._match_blocks or self._dim_match_blocks

    def update(self, markdown):  # type: ignore[no-untyped-def, override]
        # Textual's dispatcher walks the MRO and invokes every class's
        # _on_mount — overriding _on_mount and calling super() ran
        # Markdown._on_mount twice; the second pass saw _initial_markdown
        # already consumed and called update("") which removed all
        # blocks. Hook into update() instead: AwaitComplete's future
        # fires when parse+mount completes — set build_done from there.
        #
        # Reset document-scoped match state before the rebuild: a re-update
        # must not let a build_done waiter return on the prior render, and
        # must drop the previous render's match anchor.
        self.build_done.clear()
        self._first_match_block = None
        self._first_dim_match_block = None
        self._match_blocks = []
        self._dim_match_blocks = []
        self._build_gen += 1
        gen = self._build_gen
        aw = super().update(markdown)
        aw._future.add_done_callback(  # type: ignore[attr-defined]
            lambda _: self.build_done.set() if self._build_gen == gen else None
        )
        return aw

    def _parse_markdown(self, tokens):  # type: ignore[no-untyped-def, override]
        tokens = list(tokens)
        rewrite_callouts(tokens)
        # Materialise the block tree before yielding any of it: the two-tier
        # proximity decision needs the whole chunk's text at once, and right here
        # every block — including the TH/TD cells the table later composes away —
        # is still reachable and unmounted, so applying spans costs no layout.
        blocks = list(super()._parse_markdown(tokens))
        apply_chunk_highlights(self, blocks)
        return blocks


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

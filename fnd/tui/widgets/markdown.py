"""Highlight-aware Markdown widget tree for the preview pane."""

from __future__ import annotations

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
    match_word_spans,
    phrase_gap_spans,
)
from fnd.tui.mermaid_render import MermaidRenderer
from fnd.tui.syntax_theme import highlight_fenced, inline_code_spans

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
    spans: list[Span] = []
    covered: set[int] = set()
    for a, b, style in match_word_spans(plain, spec):
        spans.append(Span(a, b, style))
        covered.update(range(a, b))
    # Phrase highlighting (quoted phrase, or a stopword between content words)
    # fills only the GAPS between term spans — never overlaps them. Textual's
    # Content drops overlapping differently-styled spans, so an overlapping
    # phrase span in multi-colour mode would blank the whole word.
    for start, end in phrase_gap_spans(phrase_char_spans(plain, spec), covered):
        spans.append(Span(start, end, HIGHLIGHT_STYLE))
    return spans


def _spans_have_full_match(spans: list[Span]) -> bool:
    """True if any span carries a non-dimmed match style — i.e. a real
    (in-window) proximity hit, phrase, or plain match, as opposed to a
    proximity-dimmed out-of-window stray."""
    return any(str(s.style) not in DIM_STYLES for s in spans)


def _record_first_match(block: MarkdownBlock, spans: list[Span]) -> None:
    """If this block contains the first highlighted match in the
    document, register it on the parent ``FNDMarkdown`` so the preview
    pane can scroll to it. First-write-wins per tier: a block with a full
    (qualifying) match wins the primary slot; a block whose only matches
    are dimmed proximity strays fills a fallback slot. ``first_match_block``
    prefers the full slot, so a ``{N}``/``"a b"~N`` query lands on the real
    co-occurrence, not an earlier lone-term hit (mirrors the flat path).
    """
    if not spans:
        return
    md = block._markdown  # weakref unwrap
    if not isinstance(md, FNDMarkdown):
        return
    if _spans_have_full_match(spans):
        if md._first_match_block is None:
            md._first_match_block = block
    elif md._first_dim_match_block is None:
        md._first_dim_match_block = block


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


_MERMAID_RENDERER = MermaidRenderer()


def _fence_language(token: Any) -> str:
    """First word of the fence info string, lowercased (the language)."""
    return (getattr(token, "info", "") or "").strip().split(" ", 1)[0].lower()


def _record_fence_anchor_if_matched(widget: FNDMarkdownFence, code: str) -> None:
    """Register a rendered-diagram fence as the first-match scroll target
    when the active query matches inside its source. Diagram art carries no
    painted spans, so we only anchor — jump-to-match still scrolls here."""
    spec = getattr(widget._markdown, "match_spec", None)
    if spec is None or spec.is_empty:
        return
    spans = _build_match_spans(code, spec)
    if not spans:
        return
    md = widget._markdown
    if not isinstance(md, FNDMarkdown):
        return
    if _spans_have_full_match(spans):
        if md._first_match_block is None:
            md._first_match_block = widget
    elif md._first_dim_match_block is None:
        md._first_dim_match_block = widget


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
        if self._try_render_mermaid(token, code):
            return
        self._apply_fence_highlights()

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
        self._set_diagram_content(art)
        _record_fence_anchor_if_matched(self, code)
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
                return
            # Re-render failed — this is no longer a diagram: drop the
            # diagram-only styling (hscroll/no-wrap) before falling back.
            self.remove_class("mermaid-diagram")
            self._mermaid_code = None
        self._apply_fence_highlights()

    def _apply_fence_highlights(self) -> None:
        spec = getattr(self._markdown, "match_spec", None)
        if spec is None or spec.is_empty:
            return
        spans = _build_match_spans(self._highlighted_code.plain, spec)
        if not spans:
            return
        self._highlighted_code = self._highlighted_code.add_spans(spans)
        self.set_content(self._highlighted_code)
        _record_first_match(self, spans)


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
        spec = getattr(md, "match_spec", None) or MatchSpec()
        matches = _find_match_coords_in_table(headers, rows, spec)
        # Full-match cells (dim proximity strays skipped) so match-nav hops
        # only between genuine hits; the first match stays the scroll target.
        dt._fnd_match_coords = [Coordinate(*rc) for rc, full in matches if full]  # type: ignore[attr-defined]
        if matches:
            match_coord, is_full = matches[0]
            dt._fnd_match_coord = Coordinate(*match_coord)  # type: ignore[attr-defined]
            # Register self as parent's first_match_block — TH/TD widgets are
            # bypassed so _record_first_match never fires. A full co-occurrence
            # claims the primary slot; a dim-only table fills the fallback, so a
            # later full match elsewhere still wins the scroll target.
            if isinstance(md, FNDMarkdown):
                if is_full:
                    if md._first_match_block is None:
                        md._first_match_block = self
                elif md._first_dim_match_block is None:
                    md._first_dim_match_block = self
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

    out: list[tuple[tuple[int, int], bool]] = []
    for col, h in enumerate(headers):
        tier = _tier(getattr(h, "plain", "") or "")
        if tier is not None:
            out.append(((0, col), bool(tier)))
    for r_idx, row in enumerate(rows):
        for c_idx, cell in enumerate(row):
            tier = _tier(getattr(cell, "plain", "") or "")
            if tier is not None:
                out.append(((r_idx, c_idx), bool(tier)))
    # Full matches first (stable within tier) so the single scroll target and
    # the first nav stop prefer a real co-occurrence over a dim-only stray.
    out.sort(key=lambda t: not t[1])
    return out


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
        self._build_gen += 1
        gen = self._build_gen
        aw = super().update(markdown)
        aw._future.add_done_callback(  # type: ignore[attr-defined]
            lambda _: self.build_done.set() if self._build_gen == gen else None
        )
        return aw


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

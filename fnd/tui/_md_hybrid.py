"""W-Hybrid prototype — per-chunk hybrid widget.

Replaces the per-block widget tree (`FNDMarkdown` -> 50+ widgets)
with a small ordered list of widgets per chunk:

  * Runs of "simple" block tokens (paragraph, heading, list, blockquote,
    hr) -> a single ``Static`` rendered via ``rich.markdown.Markdown``.
  * Each table token -> one ``DataTable`` widget (W3 path).
  * Each fence/code_block -> one ``MarkdownFence`` widget (full
    syntax-highlighted, scrollable, focusable).

Match highlighting:
  * Text runs: highlight spans baked into the Rich Text passed to Static.
  * Table cells: cell's Content gets spans pre-baked by the upstream
    `build_from_token` (since we still invoke the upstream parse).
  * Fences: not currently baked (rich.syntax owns the rendering).

Each emitted widget reports its "match offset" through an
attribute hook so the existing scroll-to-match logic can find a
target without traversing a per-block tree.
"""

from __future__ import annotations

from dataclasses import dataclass

from markdown_it import MarkdownIt
from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Container
from textual.widget import Widget
from textual.widgets import DataTable, Static

from fnd.matching import MatchSpec
from fnd.render import word_highlight_runs


@dataclass(slots=True)
class _IslandRange:
    kind: str  # "text" | "table" | "fence" | "code_block"
    line_start: int  # source-line range (1-based, inclusive)
    line_end: int


def _parse_islands(md_text: str) -> list[_IslandRange]:
    """Split ``md_text`` into source-line ranges classified as text or
    interactive island (table / fence / code_block).

    Uses markdown-it's token stream because tokens carry source-line
    `map` info we can use to slice the original text faithfully.
    """
    parser = MarkdownIt("gfm-like")
    tokens = parser.parse(md_text)
    ranges: list[_IslandRange] = []
    for tok in tokens:
        if tok.type in ("table_open", "fence", "code_block") and tok.map is not None:
            kind = "table" if tok.type == "table_open" else tok.type
            line_start, line_end = tok.map[0], tok.map[1]
            ranges.append(_IslandRange(kind=kind, line_start=line_start, line_end=line_end))
        elif tok.type == "table_close":
            # markdown-it nests; table_open carries the full range so
            # table_close is redundant for slicing.
            continue
    return ranges


def _split_text_and_islands(md_text: str, islands: list[_IslandRange]) -> list[_IslandRange]:
    """Given the island ranges, fill in the text ranges between them.
    Returns the full ordered list of ranges covering the whole text.
    """
    lines = md_text.splitlines()
    total = len(lines)
    out: list[_IslandRange] = []
    cursor = 0
    for isl in islands:
        if isl.line_start > cursor:
            out.append(_IslandRange(kind="text", line_start=cursor, line_end=isl.line_start))
        out.append(isl)
        cursor = isl.line_end
    if cursor < total:
        out.append(_IslandRange(kind="text", line_start=cursor, line_end=total))
    return out


def _slice(md_text: str, rng: _IslandRange) -> str:
    return "\n".join(md_text.splitlines()[rng.line_start : rng.line_end])


def _bake_match_spans_into_text(text: Text, spec: MatchSpec) -> bool:
    """Mutate ``text`` in place, adding highlight spans for query
    matches. Returns True if any span was applied.
    """
    if spec.is_empty:
        return False
    plain = text.plain
    if not plain:
        return False
    import re

    from fnd.matching import phrase_char_spans
    from fnd.render import HIGHLIGHT_STYLE

    hit = False
    for m in re.finditer(r"\w+", plain):
        runs = word_highlight_runs(m.group(0), spec)
        for off_s, off_e, style in runs:
            text.stylize(str(style), m.start() + off_s, m.start() + off_e)
            hit = True
    for start, end in phrase_char_spans(plain, spec):
        text.stylize(HIGHLIGHT_STYLE, start, end)
        hit = True
    return hit


def _render_text_run(md_text: str, spec: MatchSpec, wrap_width: int) -> tuple[Static, bool]:
    """Render a slice of markdown text via rich.markdown into a single
    Static widget. Returns (widget, has_match)."""
    from rich.console import Console

    width = max(20, wrap_width)
    console = Console(
        width=width,
        force_terminal=True,
        color_system="truecolor",
        record=False,
    )
    md = RichMarkdown(md_text)
    options = console.options.update(width=width)
    seg_lines = console.render_lines(md, options)
    # Build a single Text combining all lines (newline-joined). Static
    # accepts a Text and lays it out as a multi-line block.
    combined = Text()
    for i, seg_line in enumerate(seg_lines):
        if i > 0:
            combined.append("\n")
        for seg in seg_line:
            if seg.text:
                combined.append(seg.text, style=seg.style if seg.style else "")
    has_match = _bake_match_spans_into_text(combined, spec)
    static = Static(combined, classes="chunk-text-run")
    return static, has_match


def _build_table_widget(md_text: str, spec: MatchSpec) -> tuple[DataTable[Text], bool]:
    """Parse a table snippet and build a DataTable widget."""
    parser = MarkdownIt("gfm-like")
    tokens = parser.parse(md_text)
    headers: list[str] = []
    rows: list[list[str]] = []
    current_row: list[str] | None = None
    in_tbody = False
    pending_cell: list[str] = []
    for tok in tokens:
        if tok.type == "tbody_open":
            in_tbody = True
        elif tok.type == "tbody_close":
            in_tbody = False
        elif tok.type == "tr_open":
            current_row = []
        elif tok.type == "tr_close":
            if in_tbody and current_row is not None:
                rows.append(current_row)
            current_row = None
        elif tok.type == "th_open" or tok.type == "td_open":
            pending_cell = []
        elif tok.type == "inline":
            # children are the inline tokens; join their content
            txt = tok.content
            pending_cell.append(txt)
        elif tok.type == "th_close":
            headers.append("".join(pending_cell))
            pending_cell = []
        elif tok.type == "td_close":
            if current_row is not None:
                current_row.append("".join(pending_cell))
            pending_cell = []
    dt: DataTable[Text] = DataTable(cursor_type="none", zebra_stripes=False, show_cursor=False)
    if headers:
        baked_headers = []
        had_header_match = False
        for h in headers:
            t = Text(h)
            if _bake_match_spans_into_text(t, spec):
                had_header_match = True
            baked_headers.append(t)
        dt.add_columns(*baked_headers)
    else:
        had_header_match = False
    has_match = had_header_match
    match_coord: tuple[int, int] | None = None
    for r_idx, row in enumerate(rows):
        baked = []
        for c_idx, cell in enumerate(row):
            t = Text(cell)
            if _bake_match_spans_into_text(t, spec):
                if match_coord is None:
                    match_coord = (r_idx, c_idx)
                has_match = True
            baked.append(t)
        dt.add_row(*baked, height=None)
    if match_coord is not None:
        from textual.coordinate import Coordinate

        dt._fnd_match_coord = Coordinate(*match_coord)  # type: ignore[attr-defined]
    return dt, has_match


def _build_fence_widget(md_text: str, spec: MatchSpec) -> tuple[Static, bool]:
    """Render a fence as syntax-highlighted ``Text`` with match spans
    overlaid on top. One Static widget per fence — fits in the parent
    ScrollView, but no horizontal scroll / focus (those are
    MarkdownFence's value-add and would require the upstream widget,
    which needs a Markdown parent).

    Lexer styles come from ``Syntax.highlight`` as a Rich ``Text``;
    match spans are baked on afterwards so the highlight reads over the
    syntax colouring (rendering the bare ``Syntax`` would drop them).
    """
    from rich.syntax import Syntax

    # Parse the fence header: ```LANG\n... -> LANG = "python", body = rest
    lines = md_text.splitlines()
    lexer = ""
    body_lines: list[str] = []
    if lines and lines[0].startswith("```"):
        lexer = lines[0][3:].strip()
        body_lines = lines[1:]
        if body_lines and body_lines[-1].strip() == "```":
            body_lines = body_lines[:-1]
    else:
        body_lines = lines
    code = "\n".join(body_lines)
    if not lexer:
        lexer = "text"
    try:
        text = Syntax(
            code, lexer, background_color="default", word_wrap=False, line_numbers=False
        ).highlight(code)
        # highlight() appends a trailing newline; drop it so plain == code
        # and match offsets line up. right_crop mutates in place (returns None).
        if text.plain.endswith("\n") and not code.endswith("\n"):
            text.right_crop(1)
    except Exception:
        text = Text(code)
    has_match = _bake_match_spans_into_text(text, spec)
    return Static(text, classes="chunk-fence-run"), has_match


def build_hybrid_chunk_widgets(
    body_md: str, spec: MatchSpec, wrap_width: int
) -> tuple[list[Widget], int | None]:
    """Build the per-chunk widget list and return the index of the
    first widget containing a match (or None if no match).
    """
    if not body_md.strip():
        return [], None
    islands = _parse_islands(body_md)
    ranges = _split_text_and_islands(body_md, islands)
    out: list[Widget] = []
    first_match_idx: int | None = None
    for rng in ranges:
        snippet = _slice(body_md, rng)
        if not snippet.strip():
            continue
        if rng.kind == "text":
            w, has_match = _render_text_run(snippet, spec, wrap_width)
        elif rng.kind == "table":
            w, has_match = _build_table_widget(snippet, spec)
        else:  # fence / code_block
            w, has_match = _build_fence_widget(snippet, spec)
        if has_match and first_match_idx is None:
            first_match_idx = len(out)
        out.append(w)
    return out, first_match_idx


class FNDChunkHybrid(Container):
    """Chunk-scoped hybrid widget: a Container of text-run Statics +
    embedded DataTable/fence widgets, built once at compose time.
    """

    DEFAULT_CSS = """
    FNDChunkHybrid {
        height: auto;
    }
    FNDChunkHybrid > .chunk-text-run { height: auto; }
    FNDChunkHybrid > .chunk-fence-run { height: auto; }
    """

    def __init__(
        self,
        body_md: str,
        *,
        match_spec: MatchSpec,
        wrap_width: int,
        classes: str | None = None,
    ) -> None:
        super().__init__(classes=classes)
        self._body_md = body_md
        self._spec = match_spec
        self._wrap_width = wrap_width
        self._first_match_widget: Widget | None = None

    def compose(self) -> ComposeResult:
        widgets, first_idx = build_hybrid_chunk_widgets(self._body_md, self._spec, self._wrap_width)
        yield from widgets
        if first_idx is not None and 0 <= first_idx < len(widgets):
            self._first_match_widget = widgets[first_idx]

    @property
    def first_match_block(self) -> Widget | None:
        return self._first_match_widget

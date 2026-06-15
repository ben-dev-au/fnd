"""W8 prototype — render md chunks to styled flat lines.

Renders each chunk's ``body_md`` (or falls back to ``body_text``) via
``rich.markdown.Markdown`` to width-wrapped styled lines, then bakes
the search-term highlight spans on top. Returns a FileView ready to
feed into the existing ``LineBufferPreview`` pipeline.
"""

from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from fnd.matching import MatchSpec
from fnd.query import FileChunk
from fnd.render import match_word_spans
from fnd.tui.line_buffer import FileView


def _bake_match_spans(line: Text, spec: MatchSpec) -> bool:
    """Apply highlight spans on ``line`` in place. Returns True if any
    match was baked."""
    if spec.is_empty:
        return False
    plain = line.plain
    if not plain:
        return False
    hit = False
    for a, b, style in match_word_spans(plain, spec):
        line.stylize(str(style), a, b)
        hit = True
    return hit


def _render_md_to_lines(md_text: str, wrap_width: int) -> list[Text]:
    """Render ``md_text`` via rich.markdown to a list of styled lines.

    Width is honoured; each line is a `Text` with original styling
    intact (bold for ``**``, dim for code, etc.). Padding/justification
    is whatever Rich emits.
    """
    if not md_text.strip():
        return []
    width = max(20, wrap_width)
    console = Console(
        width=width,
        force_terminal=True,
        color_system="truecolor",
        record=False,
    )
    md = Markdown(md_text)
    options = console.options.update(width=width)
    seg_lines = console.render_lines(md, options)
    out: list[Text] = []
    for seg_line in seg_lines:
        line_text = Text()
        for seg in seg_line:
            if seg.text:
                line_text.append(seg.text, style=seg.style if seg.style else "")
        out.append(line_text)
    return out


def build_md_file_view(
    chunks: list[FileChunk],
    *,
    spec: MatchSpec,
    wrap_width: int,
    insert_chunk_gaps: bool = True,
) -> FileView:
    """Build a :class:`FileView` from markdown chunks with rich
    rendering + match-span baking. One FileView per file.
    """
    fv = FileView()
    width = max(20, wrap_width)
    cursor = 0
    for c in chunks:
        body = c.body_md if c.body_md else "\n".join(b.text for b in c.blocks)
        rendered = _render_md_to_lines(body, width)
        if insert_chunk_gaps and fv.lines:
            fv.lines.append(Text(""))
            fv.line_to_chunk.append(c.chunk_seq)
            cursor += 1
        chunk_start = cursor
        first_hit_local: int | None = None
        for ln in rendered:
            had_match = _bake_match_spans(ln, spec)
            if had_match and first_hit_local is None:
                first_hit_local = cursor
            if had_match:
                fv.match_lines.add(cursor)
            fv.lines.append(ln)
            fv.line_to_chunk.append(c.chunk_seq)
            cursor += 1
        fv.chunk_to_range[c.chunk_seq] = (chunk_start, cursor)
        fv.structural_map.append((chunk_start, cursor, "chunk", c.chunk_seq))
        if first_hit_local is not None:
            fv.first_hit_line_in_chunk[c.chunk_seq] = first_hit_local
    return fv

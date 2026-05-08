"""Render ``body_struct`` (list of :class:`Block`) to Markdown source for
Textual's :class:`textual.widgets.Markdown` widget, with query-term highlights
applied via a Markdown bold wrap.

Per plan §5: preview should show structured text (headings / paragraphs /
lists), not a raw blob. Phase 5 ships the simplest faithful renderer; phase 7
adds match-cluster minimap.
"""

from __future__ import annotations

import re
from typing import Any

from rich.text import Text

from acorn.extract.base import Block

_HEADING_KINDS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})


def _highlight(text: str, terms: list[str]) -> str:
    """Wrap each whole-word occurrence of any term in Markdown bold (**…**).

    Case-insensitive. Long-term match wins on ties so "supersymmetry" beats
    "super" when both are queried.
    """
    if not terms:
        return text
    # Escape regex metas in terms; sort longest-first.
    safe = sorted({re.escape(t) for t in terms if t}, key=len, reverse=True)
    if not safe:
        return text
    pattern = re.compile(r"\b(" + "|".join(safe) + r")\b", re.IGNORECASE)
    return pattern.sub(lambda m: f"**{m.group(0)}**", text)


def _terms_from_query(query: str) -> list[str]:
    """Pull plain-word terms out of a query string for highlighting.

    Strips operators (AND/OR/NOT/+/-/parens/quotes/wildcards/fuzzy/range
    syntax) and field qualifiers like ``kind:pdf`` so the highlighter only
    bolds genuine search terms."""
    if not query:
        return []
    # Drop bracketed range syntax.
    q = re.sub(r"\[[^\]]*\]", " ", query)
    # Drop {N} proximity prefix.
    q = re.sub(r"\{\d+\}", " ", q)
    # Drop NEAR/N marker.
    q = re.sub(r"\bNEAR/\d+\b", " ", q)
    # Drop field qualifiers: word followed by colon then non-space.
    q = re.sub(r"\b\w+:\S+", " ", q)
    # Drop Tantivy operators / sigils.
    q = re.sub(r"[+\-()\"~*?]", " ", q)
    # Drop bare AND / OR / NOT.
    q = re.sub(r"\b(AND|OR|NOT)\b", " ", q)
    return [w for w in q.split() if w]


def render(blocks: list[Block], *, query: str = "") -> str:
    """Return Markdown source for ``blocks`` with query terms highlighted."""
    terms = _terms_from_query(query)
    parts: list[str] = []
    for b in blocks:
        text = _highlight(b.text, terms)
        if b.kind in _HEADING_KINDS:
            level = int(b.kind[1])
            parts.append(f"{'#' * level} {text}\n")
        elif b.kind == "ul":
            parts.append(f"- {text}\n")
        elif b.kind == "ol":
            parts.append(f"1. {text}\n")
        elif b.kind == "code":
            parts.append(f"```\n{b.text}\n```\n")  # don't bold inside code
        elif b.kind == "quote":
            for line in text.splitlines() or [text]:
                parts.append(f"> {line}\n")
        else:
            parts.append(f"{text}\n\n")
    return "".join(parts).strip() + "\n"


def render_document(chunks: list[Any], *, query: str = "") -> str:
    """Render a full document (sequence of chunks) with section dividers and
    every query-term occurrence bolded. Each chunk gets a section header
    (``## p.N`` for PDFs, ``## Slide N`` for PPTX, ``## heading_path`` for
    DOCX/MD) so the user sees structure as they scroll.

    ``chunks`` is a list of :class:`acorn.query.FileChunk`-shaped records
    (duck-typed to keep the renderer free of a query.py import dependency).
    """
    parts: list[str] = []
    for c in chunks:
        # Section header per chunk so structural boundaries are obvious.
        header = _chunk_header(c)
        if header:
            parts.append(f"## {header}\n\n")
        body = render(c.blocks, query=query)
        parts.append(body)
        parts.append("\n---\n\n")
    md = "".join(parts).rstrip()
    # Trim trailing horizontal rule.
    if md.endswith("---"):
        md = md[:-3].rstrip()
    return md + "\n"


def render_chunk_pieces(chunk: Any, *, query: str = "") -> tuple[Text, list[tuple[Text, bool]]]:
    """Split a chunk into a header Text plus a list of (line_text, has_match)
    pairs.

    Two modes for performance:

    * **Chunk contains a match**: emit one piece per body line so the TUI
      can mount per-line widgets and ``scroll_to_widget`` targets the
      specific matched line.
    * **Chunk has no match**: emit a single piece containing the whole
      chunk body — far fewer widgets to mount on long PDFs (a 105-page
      winelist is ~3000 widgets all-per-line vs ~280 with this split).

    The returned line Texts already have query-term highlights applied.
    """
    header = Text(f" {_chunk_header(chunk)}", style="bold #82aaff")

    terms = _terms_from_query(query)
    pattern: re.Pattern[str] | None = None
    if terms:
        sorted_terms = sorted({t for t in terms if t}, key=len, reverse=True)
        pattern = re.compile(
            r"\b(" + "|".join(re.escape(t) for t in sorted_terms) + r")\b",
            re.IGNORECASE,
        )

    chunk_has_match = bool(
        pattern and any(pattern.search(getattr(b, "text", "") or "") for b in chunk.blocks)
    )

    pieces: list[tuple[Text, bool]] = []
    if not chunk_has_match:
        # Single-piece body for performance.
        body = render_chunk_rich(chunk, query=query)
        # Strip the header from the body (render_chunk_rich prepends one).
        body_lines = body.plain.splitlines()
        if body_lines and body_lines[0].strip() == _chunk_header(chunk):
            # Rebuild without the header line.
            no_header = render_chunk_body_only(chunk, query=query)
            pieces.append((no_header, False))
        else:
            pieces.append((body, False))
        return header, pieces

    # Match-bearing chunk: per-line pieces so we have precise scroll targets.
    for b in chunk.blocks:
        kind = getattr(b, "kind", "p")
        text_value = getattr(b, "text", "") or ""
        for line in text_value.splitlines() or [text_value]:
            line = line.rstrip()
            if not line:
                continue
            if kind in _HEADING_KINDS:
                level = int(kind[1])
                rendered = Text(f"{'  ' * (level - 1)}{line}", style="bold")
            elif kind == "ul":
                rendered = Text(f"  • {line}")
            elif kind == "ol":
                rendered = Text(f"  1. {line}")
            elif kind == "code":
                rendered = Text(line, style="dim")
            elif kind == "quote":
                rendered = Text(f"  ▎ {line}", style="italic dim")
            else:
                rendered = Text(line)
            has_match = bool(pattern and pattern.search(line))
            if pattern and has_match:
                rendered.highlight_regex(pattern, style="bold black on #ffd866")
            pieces.append((rendered, has_match))
    return header, pieces


def render_chunk_body_only(chunk: Any, *, query: str = "") -> Text:
    """Render a chunk's body blocks (no header line) as a single Text."""
    text = Text()
    for b in chunk.blocks:
        kind = getattr(b, "kind", "p")
        text_value = getattr(b, "text", "") or ""
        if kind in _HEADING_KINDS:
            level = int(kind[1])
            text.append(f"{'  ' * (level - 1)}{text_value}\n", style="bold")
        elif kind == "ul":
            text.append(f"  • {text_value}\n")
        elif kind == "ol":
            text.append(f"  1. {text_value}\n")
        elif kind == "code":
            text.append(f"{text_value}\n", style="dim")
        elif kind == "quote":
            text.append(f"  ▎ {text_value}\n", style="italic dim")
        else:
            text.append(f"{text_value}\n")
    # Apply highlights so even the no-match-chunks page render is consistent.
    terms = _terms_from_query(query)
    if terms:
        sorted_terms = sorted({t for t in terms if t}, key=len, reverse=True)
        pattern = re.compile(
            r"\b(" + "|".join(re.escape(t) for t in sorted_terms) + r")\b",
            re.IGNORECASE,
        )
        text.highlight_regex(pattern, style="bold black on #ffd866")
    return text


def render_chunk_rich(chunk: Any, *, query: str = "") -> Text:
    """Render one chunk as a Rich :class:`Text` — section header + body
    blocks + query-term highlights. Used by the TUI's preview pane (one
    Static widget per chunk so scroll_to_widget targets work precisely
    regardless of how lines wrap visually)."""
    text = Text()
    header = _chunk_header(chunk)
    text.append(f" {header}\n", style="bold #82aaff")  # tokyo-night blue
    text.append("\n")
    for b in chunk.blocks:
        kind = getattr(b, "kind", "p")
        text_value = getattr(b, "text", "") or ""
        if kind in _HEADING_KINDS:
            level = int(kind[1])
            text.append(f"{'  ' * (level - 1)}{text_value}\n", style="bold")
        elif kind == "ul":
            text.append(f"  • {text_value}\n")
        elif kind == "ol":
            text.append(f"  1. {text_value}\n")
        elif kind == "code":
            text.append(f"{text_value}\n", style="dim")
        elif kind == "quote":
            text.append(f"  ▎ {text_value}\n", style="italic dim")
        else:
            text.append(f"{text_value}\n")

    terms = _terms_from_query(query)
    if terms:
        sorted_terms = sorted({t for t in terms if t}, key=len, reverse=True)
        pattern = re.compile(
            r"\b(" + "|".join(re.escape(t) for t in sorted_terms) + r")\b",
            re.IGNORECASE,
        )
        text.highlight_regex(pattern, style="bold black on #ffd866")
    return text


def render_document_rich(chunks: list[Any], *, query: str = "") -> tuple[Text, dict[int, int]]:
    """Build a single Rich :class:`Text` for the entire document plus a
    chunk-seq → line offset map. Kept for tests and any future
    "single-pane" rendering path; the TUI uses :func:`render_chunk_rich`
    plus per-chunk widgets for precise scroll instead.
    """
    text = Text()
    offsets: dict[int, int] = {}
    line = 0
    for c in chunks:
        offsets[int(getattr(c, "chunk_seq", 0))] = line
        chunk_text = render_chunk_rich(c, query=query)
        text.append(chunk_text)
        text.append("\n")
        line += chunk_text.plain.count("\n") + 1
    return text, offsets


def _chunk_header(c: object) -> str:
    """Format a section header from a FileChunk's metadata.

    Composes locator + heading when both exist (e.g. "p. 7 · 3.2 Soft
    breaking"); falls back to whichever piece is present otherwise.
    """
    page = getattr(c, "page", 0)
    slide = getattr(c, "slide", 0)
    heading_path = getattr(c, "heading_path", "")
    parts: list[str] = []
    if page:
        parts.append(f"p. {page}")
    elif slide:
        parts.append(f"Slide {slide}")
    if heading_path:
        parts.append(heading_path)
    if parts:
        return " · ".join(parts)
    seq = getattr(c, "chunk_seq", 0)
    return f"§ {seq + 1}"

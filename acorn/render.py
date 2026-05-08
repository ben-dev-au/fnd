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

import snowballstemmer
from rich.text import Text

from acorn.extract.base import Block

_HEADING_KINDS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})

# The Tantivy index uses ``en_stem`` (Snowball English) for the body field,
# so a query for "penfold" matches both "penfold" and "penfolds". The preview
# highlighter must agree: stem each query term and each document word, then
# highlight by stem-equality. (Phase 5.9 — fixes user-reported bug where
# "penfolds" matched "penfold" in results but didn't highlight.)
_STEMMER = snowballstemmer.stemmer("english")
HIGHLIGHT_STYLE = "bold black on #ffd866"


def _stem(word: str) -> str:
    return _STEMMER.stemWord(word.lower())


def _term_stems(terms: list[str]) -> set[str]:
    return {_stem(t) for t in terms if t}


def text_has_match(text: str, term_stems: set[str]) -> bool:
    """True if any whole word in ``text`` stems to one of ``term_stems``."""
    if not term_stems or not text:
        return False
    return any(_stem(m.group(0)) in term_stems for m in re.finditer(r"\w+", text))


def apply_stem_highlights(rendered: Text, term_stems: set[str]) -> bool:
    """Stylize every word in ``rendered`` whose stem matches any
    ``term_stems`` entry. Mutates ``rendered`` in place. Returns True if any
    highlight was applied."""
    if not term_stems:
        return False
    found = False
    plain = rendered.plain
    for m in re.finditer(r"\w+", plain):
        if _stem(m.group(0)) in term_stems:
            rendered.stylize(HIGHLIGHT_STYLE, m.start(), m.end())
            found = True
    return found


def _highlight(text: str, terms: list[str]) -> str:
    """Wrap each whole-word occurrence of any term in Markdown bold (**…**).

    Stem-aware: "penfold" highlights both "penfold" and "penfolds", matching
    Tantivy's ``en_stem`` tokenizer behavior. Used by the legacy Markdown
    render path (kept for export use; the TUI takes the Rich-Text path).
    """
    if not terms:
        return text
    term_stems = _term_stems(terms)
    if not term_stems:
        return text

    def _wrap(m: re.Match[str]) -> str:
        word = m.group(0)
        if _stem(word) in term_stems:
            return f"**{word}**"
        return word

    return re.sub(r"\w+", _wrap, text)


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
    term_stems = _term_stems(terms)

    chunk_has_match = bool(
        term_stems
        and any(text_has_match(getattr(b, "text", "") or "", term_stems) for b in chunk.blocks)
    )

    pieces: list[tuple[Text, bool]] = []
    if not chunk_has_match:
        # Single-piece body for performance.
        no_header = render_chunk_body_only(chunk, query=query)
        pieces.append((no_header, False))
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
            has_match = apply_stem_highlights(rendered, term_stems)
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
    apply_stem_highlights(text, _term_stems(_terms_from_query(query)))
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

    apply_stem_highlights(text, _term_stems(_terms_from_query(query)))
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

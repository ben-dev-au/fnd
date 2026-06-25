"""Render ``body_struct`` (list of :class:`Block`) to Markdown source for
Textual's :class:`textual.widgets.Markdown` widget, with query-term highlights
applied via a Markdown bold wrap.

Per plan §5: preview should show structured text (headings / paragraphs /
lists), not a raw blob. Phase 5 ships the simplest faithful renderer; phase 7
adds match-cluster minimap.
"""

from __future__ import annotations

import re
import threading
from typing import TYPE_CHECKING, Any

import snowballstemmer
from rich.text import Text

from fnd.extract.base import Block
from fnd.matching import DOC_WORD_RE
from fnd.stopwords import STOPWORDS as _HL_STOPWORDS

if TYPE_CHECKING:
    from fnd.matching import MatchSpec

_HEADING_KINDS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})

# Stem each query term and each document word so "penfold" highlights for
# both "penfold" and "penfolds" (Tantivy's en_stem on F_BODY).
# threading.local: snowballstemmer instances aren't thread-safe.
_STEMMER_LOCAL = threading.local()
HIGHLIGHT_STYLE = "bold black on #ffd866"
# Mismatch overlay for fuzzy-pass hits — orange so the eye can read
# at a glance which char(s) in a near-match diverge from what the user
# typed. Same black foreground as HIGHLIGHT_STYLE so adjacent
# yellow/orange runs feel like one painted word.
MISMATCH_STYLE = "bold black on #ff9e64"
# Per-term match palette: in a multi-word query each distinct term highlights
# in its own colour so the eye can tell which match is which. Slot 0 is
# HIGHLIGHT_STYLE, so single-term queries are unchanged. The orange
# MISMATCH_STYLE is shared across terms for the variance/wildcard-filled chars.
MATCH_STYLES = [
    HIGHLIGHT_STYLE,  # yellow
    "bold black on #7dcfff",  # cyan
    "bold black on #9ece6a",  # green
    "bold black on #bb9af7",  # purple
    "bold black on #82aaff",  # blue
]
# Dimmed ("receded") variants, used for proximity-group matches that fall OUTSIDE
# a qualifying co-occurrence window: each swatch is a ~50/50 blend of the full
# colour and the dark app background, with a normal-weight body foreground — so
# the match stays clearly visible but reads as obviously secondary to the bright,
# black-on-colour full highlights. Pre-computed (not SGR ``faint``) for
# deterministic, terminal-independent rendering. Parallel to MATCH_STYLES.
DIM_MATCH_STYLES = [
    "#c0caf5 on #8c7a46",  # yellow → muted gold
    "#c0caf5 on #4c7593",  # cyan
    "#c0caf5 on #5c7548",  # green
    "#c0caf5 on #6b5b8f",  # purple
    "#c0caf5 on #4e6393",  # blue
]
# Dimmed mismatch overlay (orange variance chars within a dimmed near-match).
DIM_MISMATCH_STYLE = "#c0caf5 on #8c5c45"
# Every receded swatch a proximity-dimmed occurrence can carry. The preview's
# auto-scroll target treats a span in this set as a non-qualifying stray, so a
# {N}/"a b"~N query lands on the real co-occurrence, not an earlier lone term.
DIM_STYLES: frozenset[str] = frozenset(DIM_MATCH_STYLES) | {DIM_MISMATCH_STYLE}


def match_style(color: int, *, dim: bool = False) -> str:
    """Match style for colour slot ``color`` (cycles through the palette).

    ``dim`` selects the receded variant for proximity matches outside a window."""
    palette = DIM_MATCH_STYLES if dim else MATCH_STYLES
    return palette[color % len(palette)]


def _stem(word: str) -> str:
    s = getattr(_STEMMER_LOCAL, "instance", None)
    if s is None:
        s = snowballstemmer.stemmer("english")
        _STEMMER_LOCAL.instance = s
    return s.stemWord(word.lower())


def _term_stems(terms: list[str]) -> set[str]:
    return {_stem(t) for t in terms if t}


def text_has_match(text: str, term_stems: set[str]) -> bool:
    """True if any whole word in ``text`` stems to one of ``term_stems``.

    Strict-stem variant kept for callers that explicitly want literal
    semantics (e.g. snippet detection that should not light up on
    fuzzy near-matches). Fuzzy / synonym-aware callers should use
    :func:`text_has_any_match` with a :class:`MatchSpec`.
    """
    if not term_stems or not text:
        return False
    return any(_stem(m.group(0)) in term_stems for m in DOC_WORD_RE.finditer(text))


def text_has_any_match(text: str, spec: MatchSpec) -> bool:
    """True if any whole word in ``text`` matches ``spec`` under any of
    the cascade's pass semantics (exact-stem, fuzzy-AUTO, synonym).
    Used by the match-aware scrollbar so its markers cover all the
    same chunks the user-visible highlights cover."""
    from fnd.matching import phrase_char_spans, word_matches

    if spec.is_empty or not text:
        return False
    if any(word_matches(m.group(0), spec) for m in DOC_WORD_RE.finditer(text)):
        return True
    return bool(phrase_char_spans(text, spec))


def text_has_full_match(text: str, spec: MatchSpec) -> bool:
    """Like :func:`text_has_any_match`, but a proximity-group word that falls
    OUTSIDE a qualifying co-occurrence window does NOT count — only a *full*
    (in-window) match or a quoted-phrase span does. Lets a preview scroll
    target prefer a real co-occurrence cell over a lone dimmed term above it.

    For a plain query this is identical to :func:`text_has_any_match` (nothing
    dims), so callers gate on ``spec.proximity_groups`` to keep the cheaper
    any-match short-circuit on the common path."""
    from fnd.matching import phrase_char_spans

    if spec.is_empty or not text:
        return False
    if any(style not in DIM_STYLES for _, _, style in match_word_spans(text, spec)):
        return True
    return bool(phrase_char_spans(text, spec))


def apply_stem_highlights(rendered: Text, term_stems: set[str]) -> bool:
    """Stylize every word in ``rendered`` whose stem matches any
    ``term_stems`` entry. Mutates ``rendered`` in place. Returns True if any
    highlight was applied. Strict-stem variant kept for callers that
    don't want fuzzy / synonym expansion (tests, exports). Live
    preview rendering should use :func:`apply_match_highlights`."""
    if not term_stems:
        return False
    found = False
    plain = rendered.plain
    for m in DOC_WORD_RE.finditer(plain):
        if _stem(m.group(0)) in term_stems:
            rendered.stylize(HIGHLIGHT_STYLE, m.start(), m.end())
            found = True
    return found


def phrase_gap_spans(
    phrase_spans: list[tuple[int, int]], covered: set[int]
) -> list[tuple[int, int]]:
    """Split each phrase span into the maximal runs of chars NOT already covered
    by a per-term span. Phrase highlighting (a stopword between content words, or
    a quoted phrase) must never *overlap* a term span: in multi-colour mode the
    phrase colour and the term colour differ, and Textual's Content drops
    overlapping differently-styled spans — so the whole word goes unhighlighted.
    Filling only the gaps (e.g. the ``in`` of ``Defence-in-Depth``) keeps every
    span non-overlapping."""
    out: list[tuple[int, int]] = []
    for start, end in phrase_spans:
        run_start: int | None = None
        for i in range(start, end):
            if i not in covered:
                if run_start is None:
                    run_start = i
            elif run_start is not None:
                out.append((run_start, i))
                run_start = None
        if run_start is not None:
            out.append((run_start, end))
    return out


def _proximity_full_indices(
    tokens: list[re.Match[str]], spec: MatchSpec
) -> tuple[frozenset[str], frozenset[int], list[str]]:
    """For a spec's proximity groups, return ``(prox_stems, full, stems_by_token)``.

    ``prox_stems`` is the union of every group's stems; ``full`` is the set of
    token indices that participate in a qualifying window for some group they
    belong to. Returns empties (and an empty ``stems_by_token``) when the query
    carries no proximity operator — so plain queries skip stemming entirely."""
    if not spec.proximity_groups:
        return frozenset(), frozenset(), []
    from fnd.matching import _stem, proximity_qualifying_indices

    stems_by_token = [_stem(m.group(0)) for m in tokens]
    prox_stems = frozenset(s for stems, _ in spec.proximity_groups for s in stems)
    full: set[int] = set()
    for group in spec.proximity_groups:
        full |= proximity_qualifying_indices(stems_by_token, group)
    return prox_stems, frozenset(full), stems_by_token


def match_word_spans(plain: str, spec: MatchSpec) -> list[tuple[int, int, str]]:
    """Absolute ``(start, end, style)`` highlight runs for every matching word in
    ``plain``, with proximity-group terms that fall OUTSIDE a qualifying
    co-occurrence window rendered in the dimmed palette.

    This is the single place the two-tier proximity decision is made. Every
    preview baker (the markdown widget, the flat/hybrid prototypes) and the
    export path routes through here, so the full-vs-dim treatment can never
    drift between rendering surfaces."""
    if spec.is_empty or not plain:
        return []
    out: list[tuple[int, int, str]] = []
    tokens = list(DOC_WORD_RE.finditer(plain))
    # ``full`` holds the token indices that DO qualify; only proximity group
    # terms consult it, so plain queries get the undimmed runs unchanged.
    prox_stems, full, stems_by_token = _proximity_full_indices(tokens, spec)
    for ti, m in enumerate(tokens):
        dim = bool(prox_stems) and stems_by_token[ti] in prox_stems and ti not in full
        for offset_start, offset_end, style in word_highlight_runs(m.group(0), spec, dim=dim):
            out.append((m.start() + offset_start, m.start() + offset_end, style))
    return out


def apply_match_highlights(rendered: Text, spec: MatchSpec) -> bool:
    """Stylize matches in ``rendered``: per-term loose words via
    match_word_spans (proximity-aware), then quoted/connector phrases in the
    GAPS between term spans (never overlapping them, so per-term colours
    survive)."""
    from fnd.matching import phrase_char_spans

    if spec.is_empty:
        return False
    found = False
    plain = rendered.plain
    covered: set[int] = set()
    for a, b, style in match_word_spans(plain, spec):
        rendered.stylize(style, a, b)
        covered.update(range(a, b))
        found = True
    for start, end in phrase_gap_spans(phrase_char_spans(plain, spec), covered):
        rendered.stylize(HIGHLIGHT_STYLE, start, end)
        found = True
    return found


def _runs_from_mask(
    mask: list[bool], hit_style: str, mismatch_style: str = MISMATCH_STYLE
) -> list[tuple[int, int, str]]:
    """Compress a per-char match mask into (start, end, style) runs: True chars
    get ``hit_style`` (the term's match colour), False chars ``mismatch_style``
    (the orange variance style, or its dimmed variant)."""
    runs: list[tuple[int, int, str]] = []
    cur_start = 0
    cur = mask[0]
    for i in range(1, len(mask)):
        if mask[i] != cur:
            runs.append((cur_start, i, hit_style if cur else mismatch_style))
            cur_start = i
            cur = mask[i]
    runs.append((cur_start, len(mask), hit_style if cur else mismatch_style))
    return runs


def word_highlight_runs(
    word: str, spec: MatchSpec, *, dim: bool = False
) -> list[tuple[int, int, str]]:
    """Per-char highlight runs for ``word``, coloured by *how* it matched:

    * wildcard / glob — literal chars in the term's colour, ``*``/``?``-filled
      chars orange;
    * exact-stem / fuzzy — chars aligning to the typed term in its colour, the
      typo / stem-suffix divergence orange;
    * regex (or any match with no clean char attribution) — whole word in colour.

    In a multi-word query each term has its own colour (see :data:`MATCH_STYLES`);
    single-term queries use slot 0 (yellow). ``dim`` selects the receded palette
    for a proximity-group word that falls outside a qualifying window.
    """
    from fnd.matching import (
        _stem,
        align_doc_word,
        closest_raw_term,
        glob_match_mask,
        match_color,
        osa_within,
        word_matches,
    )

    if spec.is_empty or not word:
        return []
    if not word_matches(word, spec):
        return []
    hit_style = match_style(match_color(word, spec), dim=dim)
    mismatch_style = DIM_MISMATCH_STYLE if dim else MISMATCH_STYLE
    # Exact-stem or fuzzy: align against the closest typed term so a typo / stem
    # suffix shows as orange. Checked BEFORE the wildcard mask so a word that
    # exactly matches a typed term (e.g. ``discount`` under ``discount discoun*``)
    # renders as a clean exact hit rather than having its tail painted as
    # wildcard variance. Only for words that matched THIS way (not regex) —
    # otherwise an unrelated raw term would paint the whole word orange.
    s = _stem(word)
    matched_exact_or_fuzzy = s in spec.exact_stems or any(
        osa_within(s, q_stem, max_dist=d) <= d for q_stem, d in spec.fuzzy_per_stem
    )
    if matched_exact_or_fuzzy:
        raw = closest_raw_term(word, spec)
        if raw is not None:
            mask = align_doc_word(word, raw)
            if mask:
                return _runs_from_mask(mask, hit_style, mismatch_style)
    # Wildcard / glob: colour literal vs wildcard-filled chars.
    for glob in spec.wildcards:
        mask = glob_match_mask(word, glob)
        if mask:
            return _runs_from_mask(mask, hit_style, mismatch_style)
    # Regex match or anything without a clean char attribution → whole-word.
    return [(0, len(word), hit_style)]


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

    return DOC_WORD_RE.sub(_wrap, text)


def _terms_from_query(query: str, *, keep_stopwords: bool = False) -> list[str]:
    """Pull plain-word terms out of a query string for highlighting.

    Strips operators (AND/OR/NOT/+/-/parens/quotes/wildcards/fuzzy/range
    syntax) and field qualifiers like ``kind:pdf`` so the highlighter only
    bolds genuine search terms. Stopwords are dropped unless ``keep_stopwords``
    (the caller building an in-context phrase needs the full word run)."""
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
    # Tokenize the same way the highlighter splits doc text (``DOC_WORD_RE``,
    # which mirrors the en_stem analyzer — splits on underscore too) so a term
    # carrying adjacent punctuation ("3." / "Monitoring,") or an underscore
    # ("recursive_directory_iterator") yields the bare sub-words — their stems
    # then match the clean doc-word stems instead of silently failing.
    # Stopwords are dropped: they carry ~zero IDF and highlighting every
    # "and"/"in" doc-wide is noise (quoted phrases keep their stopwords via the
    # separate phrase-span path).
    words = DOC_WORD_RE.findall(q)
    if keep_stopwords:
        return words
    return [w for w in words if w.lower() not in _HL_STOPWORDS]


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

    ``chunks`` is a list of :class:`fnd.query.FileChunk`-shaped records
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


def render_chunk_pieces(
    chunk: Any, *, query: str = "", match_spec: Any = None
) -> tuple[Text, list[tuple[Text, bool]]]:
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
    When ``match_spec`` is provided (an :class:`fnd.matching.MatchSpec`),
    fuzzy-AUTO and synonym variants are highlighted in addition to
    literal stems — same semantics the cascade fuzzy / synonym passes
    use. Fall-back to literal-only stem matching when ``match_spec``
    is ``None`` (preserves the old test surface).
    """
    header = Text(f" {_chunk_header(chunk)}", style="bold #82aaff")

    terms = _terms_from_query(query)
    term_stems = _term_stems(terms)

    if match_spec is not None and not match_spec.is_empty:
        chunk_has_match = any(
            text_has_any_match(getattr(b, "text", "") or "", match_spec) for b in chunk.blocks
        )
    else:
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
            if match_spec is not None and not match_spec.is_empty:
                has_match = apply_match_highlights(rendered, match_spec)
            else:
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

    PDF page locator prefers the printed ``page_label`` (e.g. "292" or
    "iv") when the PDF carries explicit labels — that's what the
    reader actually sees on the page. ``page`` (the PDF page index) is
    used only for the opener and as a fallback when no label exists.
    """
    page = getattr(c, "page", 0)
    page_label = getattr(c, "page_label", "")
    slide = getattr(c, "slide", 0)
    heading_path = getattr(c, "heading_path", "")
    parts: list[str] = []
    if page_label:
        parts.append(f"p. {page_label}")
    elif page:
        parts.append(f"p. {page}")
    elif slide:
        parts.append(f"Slide {slide}")
    if heading_path:
        parts.append(heading_path)
    if parts:
        return " · ".join(parts)
    seq = getattr(c, "chunk_seq", 0)
    return f"§ {seq + 1}"

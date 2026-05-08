"""Phase 5.9: stem-aware highlighting matches the search engine.

User-reported bug: query "penfolds" returns "penfold" hits but doesn't
highlight; query "penfold" highlights "penfold" but not the "penfold" in
"penfolds". The search uses Tantivy's en_stem (Snowball English), so the
preview highlighter has to use the same stemmer for parity.
"""

from __future__ import annotations

from rich.text import Text

from acorn.extract.base import Block
from acorn.query import FileChunk
from acorn.render import (
    HIGHLIGHT_STYLE,
    apply_stem_highlights,
    render_chunk_pieces,
    text_has_match,
)
from acorn.render import _term_stems as term_stems  # private but stable

# ── stem helpers ────────────────────────────────────────────────────


def test_term_stems_collapse_plurals() -> None:
    stems = term_stems(["penfolds"])
    assert stems == term_stems(["penfold"])
    # Other Snowball reductions land on the same stem.
    assert term_stems(["computer"]) == term_stems(["computers"])
    assert term_stems(["running"]) == term_stems(["run"])


def test_text_has_match_finds_inflected_forms() -> None:
    stems = term_stems(["penfold"])
    assert text_has_match("This is from Penfolds Estate", stems)
    assert text_has_match("Penfold's Bin 707", stems)
    assert not text_has_match("This mentions yalumba and rockford", stems)


def test_apply_stem_highlights_styles_inflected_words() -> None:
    """The user's exact case: search 'penfold' must highlight 'penfolds'."""
    rendered = Text("Tasting note: Penfolds Bin 389, also Penfold Estate.")
    matched = apply_stem_highlights(rendered, term_stems(["penfold"]))
    assert matched
    plain = rendered.plain

    highlighted_segments: list[str] = []
    for span in rendered.spans:
        if HIGHLIGHT_STYLE in str(span.style):
            highlighted_segments.append(plain[span.start : span.end].lower())
    # Both inflections get the highlight span.
    assert "penfolds" in highlighted_segments
    assert "penfold" in highlighted_segments


def test_apply_stem_highlights_handles_reverse_direction() -> None:
    """Search 'penfolds' must also highlight bare 'penfold'."""
    rendered = Text("Penfold and Penfolds appear here.")
    matched = apply_stem_highlights(rendered, term_stems(["penfolds"]))
    assert matched
    plain = rendered.plain
    seen: set[str] = set()
    for span in rendered.spans:
        if HIGHLIGHT_STYLE in str(span.style):
            seen.add(plain[span.start : span.end].lower())
    assert {"penfold", "penfolds"}.issubset(seen)


def test_apply_stem_highlights_case_insensitive() -> None:
    rendered = Text("PENFOLDS Penfolds penfold")
    matched = apply_stem_highlights(rendered, term_stems(["penfold"]))
    assert matched
    plain = rendered.plain
    seen: set[str] = set()
    for span in rendered.spans:
        if HIGHLIGHT_STYLE in str(span.style):
            seen.add(plain[span.start : span.end])
    assert seen == {"PENFOLDS", "Penfolds", "penfold"}


# ── end-to-end through render_chunk_pieces ─────────────────────────


def test_render_chunk_pieces_highlights_inflected_match_and_marks_line() -> None:
    """Within a chunk, lines containing inflected matches must be flagged
    has_match=True and carry the highlight style."""
    chunk = FileChunk(
        parent_id="x",
        path="/x.pdf",
        kind="pdf",
        page=6,
        slide=0,
        heading_path="",
        chunk_seq=5,
        blocks=[
            Block(
                kind="p",
                text=(
                    "no match on this line\n"
                    "Penfolds Bin 707 mentioned here\n"
                    "another line no match\n"
                    "And here we list Penfold Estate\n"
                ),
            )
        ],
    )
    _header, pieces = render_chunk_pieces(chunk, query="penfold")
    flags = [has for _, has in pieces]
    assert flags == [False, True, False, True]
    # Lines 1 and 3 carry highlight spans.
    for line_text, _ in [pieces[1], pieces[3]]:
        styled = [
            line_text.plain[s.start : s.end]
            for s in line_text.spans
            if HIGHLIGHT_STYLE in str(s.style)
        ]
        assert styled, f"expected at least one highlighted span in {line_text.plain!r}"


def test_render_chunk_pieces_uses_query_stem_for_match_detection() -> None:
    """Verify the chunk-has-match short-circuit uses stem equality:
    a chunk whose only relevant word is 'penfolds' must split into
    per-line pieces when the query is 'penfold'."""
    chunk = FileChunk(
        parent_id="x",
        path="/x.pdf",
        kind="pdf",
        page=1,
        slide=0,
        heading_path="",
        chunk_seq=0,
        blocks=[Block(kind="p", text="alpha\nPenfolds Estate\nbravo")],
    )
    _header, pieces = render_chunk_pieces(chunk, query="penfold")
    # Match-bearing chunks split into per-line pieces.
    assert len(pieces) == 3
    assert [has for _, has in pieces] == [False, True, False]

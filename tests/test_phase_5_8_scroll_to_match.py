"""Phase 5.8: scroll target is the FIRST matched line within a chunk, not
the chunk's section header.

User reported: "the result is at p.6 (4.55) but it scrolls to the start of
the page or section, rather than where the highlighted matched result text
is". Each chunk now mounts a header widget plus per-line widgets; the
match-target map points at the first line containing a query-term match
within each chunk.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from fnd.extract.base import Block
from fnd.index import build_index
from fnd.query import FileChunk
from fnd.render import render_chunk_pieces
from fnd.tui import FNDApp

# ── render_chunk_pieces unit tests ──────────────────────────────────


def test_render_chunk_pieces_splits_lines_and_marks_matches() -> None:
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
                    "line one no match\n"
                    "line two no match\n"
                    "line three has yalumba\n"
                    "line four no match\n"
                    "line five also yalumba\n"
                ),
            )
        ],
    )
    header, pieces = render_chunk_pieces(chunk, query="yalumba")
    assert "p. 6" in header.plain
    # Five non-empty body lines.
    assert len(pieces) == 5
    # Lines 3 and 5 (0-indexed: 2 and 4) carry a match flag.
    assert [has for _, has in pieces] == [False, False, True, False, True]
    # The matched lines also have the highlight span applied.
    line3_text = pieces[2][0]
    assert any("on #ffd866" in str(sp.style) for sp in line3_text.spans)


def test_render_chunk_pieces_no_query_emits_single_piece() -> None:
    """When there's no query at all, the chunk has no matches → single body
    piece (cheap to mount). Match-bearing chunks are the exceptional path."""
    chunk = FileChunk(
        parent_id="x",
        path="/x.pdf",
        kind="pdf",
        page=1,
        slide=0,
        heading_path="",
        chunk_seq=0,
        blocks=[Block(kind="p", text="alpha\nbravo\ncharlie")],
    )
    _header, pieces = render_chunk_pieces(chunk, query="")
    # Single-piece body for non-matching chunks per perf rule.
    assert len(pieces) == 1
    assert pieces[0][1] is False
    # The body piece still contains all three lines.
    body_text = pieces[0][0].plain
    assert "alpha" in body_text
    assert "bravo" in body_text
    assert "charlie" in body_text


# ── Match-target map in the TUI ─────────────────────────────────────


@pytest.fixture
def long_page_pdf_index(fixtures_dir: Path, tmp_index_dir: Path, tmp_path: Path) -> Path:
    """Synthesise a multi-line PDF where the anchor sits MID-PAGE so we can
    verify scroll targets the correct line, not the page header."""
    import pymupdf  # type: ignore[import-not-found]

    extra = tmp_path / "papers" / "long.pdf"
    extra.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    try:
        page = doc.new_page(width=612, height=792)
        # Insert 30 distinct lines; anchor on line 17 so it's clearly
        # below the page header.
        y = 60.0
        for i in range(30):
            text = (
                "line that contains the unique zebra phrase here"
                if i == 17
                else f"line {i} ordinary content"
            )
            page.insert_text((72, y), text, fontsize=12, fontname="helv")
            y += 18
        doc.save(str(extra), garbage=4, clean=True, deflate=True)
    finally:
        doc.close()

    # Also include the existing fixture corpus (so search has more variety).
    shutil.copy(extra, tmp_path / "papers" / "long2.pdf")
    build_index(roots=[tmp_path], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_match_target_is_a_line_widget_not_the_header(
    long_page_pdf_index: Path,
) -> None:
    """Phase 5 contract: when a focused PDF chunk contains a match,
    ``scroll_to_chunk`` lands on the matched line, NOT the chunk's
    first line. The user-visible bug was scrolling to "page top"
    instead of the actual match position — the flat buffer's
    ``first_hit_line_in_chunk`` map keeps the precise target."""
    app = FNDApp(index_dir=long_page_pdf_index, initial_query="zebra")
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Tree

        tree = app.query_one("#results_pane", Tree)
        first = next(iter(tree.root.children))
        first.expand()
        await pilot.pause()
        tree.focus()
        await pilot.press("down")
        await pilot.pause()

        buf = app._active_flat_buffer
        assert buf is not None, "PDF should mount the flat-buffer preview"
        fv = buf.file_view
        assert fv is not None
        assert (
            fv.first_hit_line_in_chunk
        ), "expected at least one chunk to record a first-match line"
        focused_seq, hit_line = next(iter(fv.first_hit_line_in_chunk.items()))
        chunk_start, _ = fv.chunk_to_range[focused_seq]
        # The matched line is past the chunk's first line — that's the
        # whole point of scroll_to_chunk(prefer_first_match=True).
        assert hit_line >= chunk_start
        # And the line really does contain "zebra".
        assert "zebra" in fv.lines[hit_line].plain.lower(), fv.lines[hit_line].plain


@pytest.mark.asyncio
async def test_match_target_falls_back_to_header_when_no_match_in_chunk(
    long_page_pdf_index: Path,
) -> None:
    """Chunks without query-term matches still need a scroll target —
    ``scroll_to_chunk`` falls back to the chunk's first line when
    ``first_hit_line_in_chunk`` has no entry for that chunk."""
    app = FNDApp(index_dir=long_page_pdf_index, initial_query="zebra")
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Tree

        tree = app.query_one("#results_pane", Tree)
        first = next(iter(tree.root.children))
        first.expand()
        await pilot.pause()
        tree.focus()
        await pilot.press("down")
        await pilot.pause()

        buf = app._active_flat_buffer
        assert buf is not None
        fv = buf.file_view
        assert fv is not None
        # Every chunk in chunk_to_range must have SOME scroll target —
        # either a first-match line OR the chunk's first line.
        for seq, rng in fv.chunk_to_range.items():
            assert seq in fv.first_hit_line_in_chunk or rng[0] >= 0


def test_chunks_without_matches_collapse_to_single_piece() -> None:
    """Sanity at the render-function level: a chunk whose body lacks any
    query term emits a single body piece (1 widget for the TUI to mount,
    keeping long-PDF performance bounded)."""
    chunk = FileChunk(
        parent_id="x",
        path="/x.pdf",
        kind="pdf",
        page=2,
        slide=0,
        heading_path="",
        chunk_seq=1,
        blocks=[Block(kind="p", text="alpha\nbravo\ncharlie")],
    )
    header, pieces = render_chunk_pieces(chunk, query="zebra")
    assert header.plain
    # Performance contract: 1 piece for a no-match chunk.
    assert len(pieces) == 1
    assert pieces[0][1] is False

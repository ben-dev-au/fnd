"""Full-document preview with all matches highlighted."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.extract.base import Block
from fnd.index import build_index
from fnd.query import FileChunk, Searcher
from fnd.render import render_document
from fnd.tui import FNDApp


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


# ── Searcher.get_file_chunks ────────────────────────────────────────


def test_get_file_chunks_returns_all_pages_in_order(built_index: Path) -> None:
    """The PDF fixture has 12 pages — all should come back in chunk_seq order."""
    s = Searcher(index_dir=built_index)
    # Find the PDF's parent_id via a regular search.
    hits = s.search("blue penguin sandwich", limit=1)
    assert hits
    pdf_pid = hits[0].parent_id

    chunks = s.get_file_chunks(pdf_pid)
    assert len(chunks) == 12
    pages = [c.page for c in chunks]
    assert pages == sorted(pages), f"chunks not ordered: {pages}"
    assert pages == list(range(1, 13))


def test_get_file_chunks_carries_body_struct(built_index: Path) -> None:
    s = Searcher(index_dir=built_index)
    hits = s.search("blue penguin sandwich", limit=1)
    pdf_pid = hits[0].parent_id
    chunks = s.get_file_chunks(pdf_pid)
    # Page 7 contains the anchor; its body_struct should include a paragraph
    # block that carries the phrase.
    page7 = next(c for c in chunks if c.page == 7)
    assert page7.blocks
    text_concat = "\n".join(b.text for b in page7.blocks)
    assert "blue penguin sandwich" in text_concat


def test_get_file_chunks_unknown_id_returns_empty(built_index: Path) -> None:
    s = Searcher(index_dir=built_index)
    chunks = s.get_file_chunks("not-a-real-parent-id")
    assert chunks == []


def test_get_file_chunks_dedups_file_shared_across_collections(
    fixtures_dir: Path, tmp_index_dir: Path
) -> None:
    """A file covered by two collections (e.g. an Obsidian vault listed
    under nested source roots) is stored once per collection. The
    full-document preview must still return each chunk_seq exactly once —
    otherwise every chunk renders twice in the preview pane.
    """
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="alpha")
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="beta")

    s = Searcher(index_dir=tmp_index_dir)
    hits = s.search("blue penguin sandwich", limit=1)
    assert hits
    pid = hits[0].parent_id

    chunks = s.get_file_chunks(pid)
    seqs = [c.chunk_seq for c in chunks]
    assert len(seqs) == len(set(seqs)), f"duplicated chunk_seqs: {seqs}"
    assert seqs == sorted(seqs)


# ── render_document ─────────────────────────────────────────────────


def test_render_document_emits_section_headers_per_chunk() -> None:
    chunks = [
        FileChunk(
            parent_id="x",
            path="/x.pdf",
            kind="pdf",
            page=1,
            slide=0,
            heading_path="",
            chunk_seq=0,
            blocks=[Block(kind="p", text="alpha")],
        ),
        FileChunk(
            parent_id="x",
            path="/x.pdf",
            kind="pdf",
            page=2,
            slide=0,
            heading_path="",
            chunk_seq=1,
            blocks=[Block(kind="p", text="beta")],
        ),
    ]
    md = render_document(chunks, query="alpha")
    # Two section headers, "## p. 1" and "## p. 2".
    assert "## p. 1" in md
    assert "## p. 2" in md
    # alpha highlighted; beta not.
    assert "**alpha**" in md
    assert "**beta**" not in md


def test_render_document_uses_heading_path_when_available() -> None:
    chunks = [
        FileChunk(
            parent_id="y",
            path="/y.docx",
            kind="docx",
            page=0,
            slide=0,
            heading_path="Methods Document > Sampling",
            chunk_seq=0,
            blocks=[Block(kind="h2", text="Sampling"), Block(kind="p", text="lorem ipsum")],
        ),
    ]
    md = render_document(chunks, query="")
    assert "## Methods Document > Sampling" in md


def test_render_document_highlights_every_match_across_chunks() -> None:
    """All occurrences across all chunks must be bolded — not just the first."""
    chunks = [
        FileChunk(
            parent_id="z",
            path="/z.pdf",
            kind="pdf",
            page=p,
            slide=0,
            heading_path="",
            chunk_seq=p - 1,
            blocks=[Block(kind="p", text=f"page {p} mentions susy on each line")],
        )
        for p in range(1, 4)
    ]
    md = render_document(chunks, query="susy")
    assert md.count("**susy**") == 3


# ── TUI: full-doc preview wired correctly ──────────────────────────


@pytest.mark.asyncio
async def test_tui_renders_full_document_when_section_focused(built_index: Path) -> None:
    """Contract: focused PDF file mounts ONE LineBufferPreview;
    every chunk's line range registers in ``chunk_to_range`` and every
    anchor term carries a match style baked into the FileView's lines."""
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
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

        # The PDF takes the flat-buffer path; an active LineBufferPreview
        # holds the whole file.
        buf = app._flat.active_buffer
        assert buf is not None, "PDF should mount the flat-buffer preview"
        fv = buf.file_view
        assert fv is not None
        # 12 PDF pages → 12 chunk ranges in the FileView.
        assert len(fv.chunk_to_range) == 12, fv.chunk_to_range

        # Every anchor term should appear with a baked-in match line
        # (the line carries the match-line accent style + bold spans).
        anchor_terms = {"blue", "penguin", "sandwich"}
        seen_terms: set[str] = set()
        for li in fv.match_lines:
            plain = fv.lines[li].plain.lower()
            for term in anchor_terms:
                if term in plain:
                    seen_terms.add(term)
        assert seen_terms == anchor_terms, seen_terms

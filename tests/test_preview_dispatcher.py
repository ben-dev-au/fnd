"""Phase 5: ``choose_preview_mode`` routes chunks to the right pipeline.

Pure-function tests on the dispatcher — no Textual pilot needed. The
host wire-in (``fnd/tui/app.py``) calls this once per file-load to
decide between the flat buffer (PDF / TXT) and the structural Markdown
renderer (MD / DOCX / PPTX).
"""

from __future__ import annotations

from fnd.extract.base import Block
from fnd.query import FileChunk
from fnd.tui.preview_dispatcher import choose_preview_mode


def _chunk(kind: str, body_md: str = "") -> FileChunk:
    return FileChunk(
        parent_id="x",
        path=f"/x.{kind}",
        kind=kind,
        page=0,
        slide=0,
        heading_path="",
        chunk_seq=0,
        blocks=[Block(kind="p", text="hello")],
        body_md=body_md,
    )


def test_pdf_chunks_take_flat_path() -> None:
    assert choose_preview_mode([_chunk("pdf"), _chunk("pdf")]) == "flat"


def test_txt_chunks_take_flat_path() -> None:
    assert choose_preview_mode([_chunk("txt")]) == "flat"


def test_markdown_with_body_md_takes_structural_path() -> None:
    assert choose_preview_mode([_chunk("md", body_md="# heading")]) == "structural"


def test_docx_with_body_md_takes_structural_path() -> None:
    assert choose_preview_mode([_chunk("docx", body_md="paragraph")]) == "structural"


def test_pptx_with_body_md_takes_structural_path() -> None:
    assert choose_preview_mode([_chunk("pptx", body_md="slide title")]) == "structural"


def test_markdown_kind_without_body_md_falls_through_to_flat() -> None:
    """Stale-index defence — a markdown-kinded chunk with empty
    ``body_md`` shouldn't mount through the structural renderer (which
    would render nothing). The flat path renders the legacy ``blocks``
    text safely instead."""
    assert choose_preview_mode([_chunk("md", body_md="")]) == "flat"


def test_mixed_file_picks_structural_when_any_chunk_is_markdown() -> None:
    """A file with one structural chunk and one stale flat chunk takes
    the structural path so the user sees structure on the chunks that
    have it. Mixed-mode files are rare but real (legacy indexes)."""
    chunks = [_chunk("md", body_md=""), _chunk("md", body_md="# real heading")]
    assert choose_preview_mode(chunks) == "structural"


def test_empty_chunks_takes_flat_path() -> None:
    """Empty-state path: no chunks → flat (the flat path no-ops cleanly
    on an empty FileView; the structural path would try to mount nothing
    and surface a less useful error)."""
    assert choose_preview_mode([]) == "flat"

"""Phase 5: ``choose_preview_mode`` routes chunks to the right pipeline.

Pure-function tests on the dispatcher — no Textual pilot needed. The
host wire-in (``fnd/tui/app.py``) calls this once per file-load to
decide between the flat buffer (PDF / TXT) and the structural Markdown
renderer (MD / DOCX / PPTX).
"""

from __future__ import annotations

import pytest

from fnd.extract.base import Block
from fnd.query import FileChunk
from fnd.tui.preview_dispatcher import choose_preview_mode, uses_markdown_renderer


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
    """F8: without the pdf-structure extra, ``body_md`` stays empty and
    PDFs continue to render via the flat-buffer pipeline."""
    assert choose_preview_mode([_chunk("pdf"), _chunk("pdf")]) == "flat"


def test_pdf_with_body_md_takes_structural_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """F7: with the pdf-structure extra populating ``body_md``, PDFs
    route to the structural Markdown renderer like docx/pptx/md do."""
    monkeypatch.delenv("_FND_FORCE_FLAT", raising=False)
    chunks = [_chunk("pdf", body_md="## Page 1\n\nIntroduction.")]
    assert choose_preview_mode(chunks) == "structural"


def test_pdf_partially_structured_still_structural(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PDF that was reindexed with extras present but had an
    individual page fail extraction (empty body_md) still routes
    structural overall — the any-chunk rule wins."""
    monkeypatch.delenv("_FND_FORCE_FLAT", raising=False)
    chunks = [_chunk("pdf", body_md=""), _chunk("pdf", body_md="## p2")]
    assert choose_preview_mode(chunks) == "structural"


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


def test_uses_markdown_renderer_pdf_with_body_md() -> None:
    """Per-chunk routing must agree with ``choose_preview_mode`` — PDF
    chunks with ``body_md`` route through the markdown renderer at mount
    time. Drift between this helper and ``_MARKDOWN_RENDERED_KINDS``
    caused PDFs to render flat despite the dispatcher selecting
    structural mode."""
    assert uses_markdown_renderer(_chunk("pdf", body_md="# heading")) is True
    assert uses_markdown_renderer(_chunk("pdf", body_md="")) is False
    assert uses_markdown_renderer(_chunk("md", body_md="# h")) is True
    assert uses_markdown_renderer(_chunk("txt", body_md="ignored")) is False


def test_an_oversized_chunk_is_routed_away_from_the_markdown_renderer() -> None:
    """The size cap had no test at all — neither the routing nor the fallback.

    A chunk over the cap must stop using the structural renderer, and its text
    must still reach the flat path: routing it away is only acceptable because
    the content still renders, so both halves are asserted here.
    """
    from fnd.tui.preview_dispatcher import MARKDOWN_MAX_CHARS

    small = _chunk(kind="md", body_md="| a | b |\n|---|---|\n| 1 | 2 |")
    assert uses_markdown_renderer(small)

    row = "| kryptonwidget | beta gamma | delta |\n"
    huge = _chunk(kind="md", body_md=row * (MARKDOWN_MAX_CHARS // len(row) + 50))
    assert len(huge.body_md) > MARKDOWN_MAX_CHARS
    assert not uses_markdown_renderer(huge), (
        "a chunk far over the cap still routes to the structural renderer, which "
        "is the multi-second build the cap exists to avoid"
    )
    # A file holding only over-cap chunks must still get a preview.
    assert choose_preview_mode([huge]) == "flat"
    assert "kryptonwidget" in huge.body_md

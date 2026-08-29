"""A ``.pdf``-suffixed file with no PDF structure must fail fast, before it
ever reaches the extraction subprocess pool.

Content with no ``%PDF-`` header (plain text, null bytes) can crash the
pool worker on the way back from a clean, correctly-raised ExtractError —
traced via FND_WORKER_TRACE against real security-course test fixtures that
are named ``*.pdf`` but hold no PDF bytes at all. A magic-header check ahead
of dispatch avoids the crash-prone path entirely for such content.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.extract.base import ExtractError


def test_non_pdf_content_is_rejected_before_pool_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fnd.extract import pdf

    fake = tmp_path / "fake.pdf"
    fake.write_bytes(b"\x00" * 64)

    def _must_not_dispatch(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("extract() dispatched non-PDF content to the subprocess pool")

    monkeypatch.setattr(
        "fnd.extract._worker.run_in_pool_sync_with_stall_detection", _must_not_dispatch
    )

    with pytest.raises(ExtractError, match="not a PDF"):
        list(pdf.extract(fake))


def test_plain_text_named_pdf_is_rejected(tmp_path: Path) -> None:
    from fnd.extract import pdf

    fake = tmp_path / "fake.pdf"
    fake.write_text(" " * 200, encoding="utf-8")

    with pytest.raises(ExtractError, match="not a PDF"):
        list(pdf.extract(fake))


def test_real_pdf_header_still_dispatches(tmp_path: Path) -> None:
    """The header check must not reject genuine PDF bytes — even an
    otherwise-truncated file with a real ``%PDF-`` header should still
    reach pymupdf, which raises its own (different) error."""
    from fnd.extract import pdf

    truncated = tmp_path / "truncated.pdf"
    truncated.write_bytes(b"%PDF-1.4\ngarbage, not a real xref table")

    with pytest.raises(ExtractError) as excinfo:
        list(pdf.extract(truncated))
    assert "not a PDF" not in str(excinfo.value)

"""Adversarial-input regression tests for extractors.

Pins the audit guarantees:

- Decompression-bomb thresholds reject pathological OOXML before any
  parser runs. (M5)
- Parser crashes on malformed inputs surface as ``ExtractError`` so a
  single bad file doesn't abort the index build. (M6)
- Encrypted/unreadable OOXML packages and password-protected PDFs are
  refused with a clear reason rather than crashing or silently
  returning empty text. (S6)
- The index loop catches ``ExtractError`` and continues indexing. (M6)
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from fnd.extract import ExtractError, _ooxml, docx, extract, markdown, pdf, pptx
from fnd.extract._ooxml import reject_if_zip_bomb
from fnd.index import build_index


def test_zip_bomb_total_size_rejected(tmp_path: Path) -> None:
    bomb = tmp_path / "huge.docx"
    # Patch the limit down so the test is cheap, then write an
    # *uncompressed* (ZIP_STORED) archive whose payload exceeds it. Using
    # ZIP_STORED keeps the per-entry ratio at 1, so this test exercises
    # the total-size check in isolation.
    small_limit = 4 * 1024
    with patch.object(_ooxml, "LIMIT_OOXML_TOTAL_UNCOMPRESSED", small_limit):
        with zipfile.ZipFile(bomb, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("part1.xml", b"a" * (small_limit + 1024))
        with pytest.raises(ExtractError, match="uncompressed size"):
            reject_if_zip_bomb(bomb)


def test_zip_bomb_ratio_rejected(tmp_path: Path) -> None:
    bomb = tmp_path / "ratio.docx"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
        # A 16 KB run of repeating bytes compresses to a few bytes —
        # natural ratio well over 200×.
        zf.writestr("bomb.xml", b"A" * 16384)
    with pytest.raises(ExtractError, match="ratio"):
        reject_if_zip_bomb(bomb)


def test_not_a_zip_is_rejected(tmp_path: Path) -> None:
    f = tmp_path / "fake.docx"
    f.write_bytes(b"not a zip")
    with pytest.raises(ExtractError, match="not a valid OOXML zip"):
        reject_if_zip_bomb(f)


def test_docx_parser_crash_becomes_extract_error(tmp_path: Path) -> None:
    """A ZIP that passes the bomb precheck but isn't actually a valid
    OOXML package raises ``PackageNotFoundError`` inside ``python-docx``
    — we convert it to ExtractError."""
    f = tmp_path / "fake.docx"
    with zipfile.ZipFile(f, "w") as zf:
        zf.writestr("not-the-right-parts.xml", b"<x/>")
    with pytest.raises(ExtractError, match="docx"):
        list(docx.extract(f))


def test_pptx_parser_crash_becomes_extract_error(tmp_path: Path) -> None:
    f = tmp_path / "fake.pptx"
    with zipfile.ZipFile(f, "w") as zf:
        zf.writestr("not-the-right-parts.xml", b"<x/>")
    with pytest.raises(ExtractError, match="pptx"):
        list(pptx.extract(f))


def test_pdf_garbage_input_becomes_extract_error(tmp_path: Path) -> None:
    f = tmp_path / "fake.pdf"
    f.write_bytes(b"not a pdf at all" * 64)
    with pytest.raises(ExtractError):
        list(pdf.extract(f))


def test_markdown_invalid_utf8_becomes_extract_error(tmp_path: Path) -> None:
    f = tmp_path / "bad.md"
    # Lone continuation byte — invalid UTF-8.
    f.write_bytes(b"# hi\n\x80\x80\x80\n")
    with pytest.raises(ExtractError, match="utf-8"):
        list(markdown.extract(f))


def test_pdf_encrypted_rejected(tmp_path: Path) -> None:
    """Build a tiny encrypted PDF via pymupdf and confirm we reject it
    rather than feed it through the rest of the pipeline."""
    import pymupdf  # type: ignore[import-not-found]

    enc = tmp_path / "locked.pdf"
    doc = pymupdf.open()
    doc.new_page()
    # pymupdf.PDF_ENCRYPT_AES_256 is the right symbol but its stubs
    # don't expose it under pyright-strict; the integer is stable.
    aes_256_encryption: int = 5
    doc.save(
        str(enc),
        encryption=aes_256_encryption,
        owner_pw="owner",
        user_pw="user",
    )
    doc.close()

    with pytest.raises(ExtractError, match="encrypted"):
        list(pdf.extract(enc))


def test_index_continues_past_extract_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A poisoned file in a directory must NOT abort indexing of the
    surviving good files."""
    good = tmp_path / "good.md"
    good.write_text("# Heading\n\nSome body text.\n", encoding="utf-8")

    poison = tmp_path / "poison.docx"
    with zipfile.ZipFile(poison, "w") as zf:
        zf.writestr("noise.xml", b"<x/>")

    index_dir = tmp_path / "index"
    written = build_index(roots=[tmp_path], index_dir=index_dir, collection="default")
    captured = capsys.readouterr()

    assert written >= 1, "good.md should still index"
    assert "[fnd skip]" in captured.err
    assert "poison.docx" in captured.err


def test_extract_module_dispatch_propagates_extract_error(tmp_path: Path) -> None:
    """The public ``extract()`` dispatcher must surface the inner
    extractor's ExtractError unchanged."""
    bad = tmp_path / "x.docx"
    bad.write_bytes(b"garbage")
    with pytest.raises(ExtractError):
        list(extract(bad))


def test_index_drops_partial_chunks_on_midstream_extract_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An extractor that yields chunks and *then* raises ExtractError
    must leave nothing behind for that file. Pre-fix, chunks added
    before the crash (and potentially committed by the mid-loop batch
    commit) would survive."""
    from collections.abc import Iterator

    from fnd import index as index_module
    from fnd.extract.base import Chunk
    from fnd.query import Searcher

    poison = tmp_path / "midstream.md"
    poison.write_text("# heading\n\nbody\n", encoding="utf-8")
    good = tmp_path / "ok.md"
    good.write_text("# ok\n\nokbody\n", encoding="utf-8")

    real_extract = index_module.extract

    def flaky_extract(path: Path) -> Iterator[Chunk]:
        if path.name != "midstream.md":
            yield from real_extract(path)
            return
        # Yield two well-formed chunks for the poison file, *then* raise.
        # Two is enough to exercise the "some chunks already buffered
        # in the writer" path without depending on _COMMIT_BATCH.
        yield Chunk(
            parent_id=index_module._path_parent_id(path),
            path=str(path),
            mtime=1,
            kind="md",
            body="partial-chunk-1",
            chunk_seq=0,
        )
        yield Chunk(
            parent_id=index_module._path_parent_id(path),
            path=str(path),
            mtime=1,
            kind="md",
            body="partial-chunk-2",
            chunk_seq=1,
        )
        raise ExtractError(str(path), "synthetic mid-iteration failure")

    monkeypatch.setattr(index_module, "extract", flaky_extract)

    index_dir = tmp_path / "index"
    build_index(roots=[tmp_path], index_dir=index_dir, collection="default")

    searcher = Searcher(index_dir=index_dir)
    # The poison file's body never wins a hit — its (partial) chunks
    # should have been cleaned up in the except branch.
    assert searcher.search("partial-chunk-1") == []
    assert searcher.search("partial-chunk-2") == []
    # The good file's chunk survives normally.
    assert searcher.search("okbody")

"""PDF own-heading is folded into ``body`` even on a texture-cache HIT.

The PDF texture cache stores whole chunks (including ``body``) keyed by
content hash, and durably reuses prior entries across signature bumps.
So the heading-into-body fix must apply when serving cached chunks too —
otherwise a plain ``reindex --rebuild`` would keep serving the old
un-folded body and the fix would require a full re-texture to land.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from fnd.cache import ExtractionCache, sha256_file
from fnd.extract import pdf
from fnd.extract.base import Block, Chunk


def _make_pdf(path: Path) -> None:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Generic body paragraph only.")
    doc.save(str(path))
    doc.close()


def test_cached_chunk_gets_heading_folded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf_path = tmp_path / "doc.pdf"
    _make_pdf(pdf_path)
    content_sha = sha256_file(pdf_path)

    # Seed the cache with an OLD-style chunk: heading present in
    # heading_path, but NOT in body (the pre-fix shape).
    cache = ExtractionCache(root=tmp_path / "cache")
    stale = Chunk(
        parent_id="old",
        path="/old.pdf",
        mtime=1,
        kind="pdf",
        body="Generic body paragraph only.",
        body_struct=[Block(kind="p", text="Generic body paragraph only.")],
        # Textured: an entry with no body_md is refused as a stale texturising,
        # and this test needs a HIT.
        body_md="Generic body paragraph only.",
        heading_path="Chapter 9 > Cached Heading Wombat",
        page=1,
        chunk_seq=0,
    )
    cache.put(cache.build_key(content_sha256=content_sha, extractor_signature="old-sig"), [stale])

    monkeypatch.setattr(pdf, "_cache_singleton", cache)
    monkeypatch.setattr(pdf, "_force_fresh_texture", False)

    chunks = list(pdf.extract(pdf_path))
    assert len(chunks) == 1
    assert "Cached Heading Wombat" in chunks[0].body

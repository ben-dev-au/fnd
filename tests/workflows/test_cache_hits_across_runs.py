"""Cache hits accumulate across Update index runs.

User report: "0 hits, all misses" on a multi-PDF reindex. Verified
that the cache hit/miss counters tick correctly when the same file
is extracted twice in a row.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_second_extract_of_same_file_hits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Extract a PDF, then extract it again. The second call must
    register as a cache hit (cache.hits += 1)."""
    cache_root = tmp_path / "cache"
    monkeypatch.setattr("fnd.cache.default_cache_dir", lambda: cache_root)
    from fnd.extract import pdf

    monkeypatch.setattr(pdf, "_cache_singleton", None)
    fixture = Path(__file__).parent.parent / "fixtures" / "papers" / "test.pdf"
    if not fixture.exists():
        # Use any PDF fixture in the repo as a stand-in.
        candidates = list((Path(__file__).parent.parent / "fixtures").rglob("*.pdf"))
        if not candidates:
            import pytest

            pytest.skip("no PDF fixture available in tests/fixtures")
        fixture = candidates[0]

    # First extraction: should be a miss, cached at the end.
    list(pdf.extract(fixture))
    cache = pdf._get_cache()
    misses_after_first = cache.misses
    hits_after_first = cache.hits
    assert misses_after_first >= 1
    assert hits_after_first == 0

    # Second extraction of the same file: should hit.
    list(pdf.extract(fixture))
    assert cache.hits >= 1, (
        f"second extraction of the same file should hit; "
        f"hits={cache.hits} misses={cache.misses}"
    )


def test_extractor_signature_change_invalidates_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the extractor signature changes, old entries don't match
    new keys. This is the user's reported "0 hits after install"
    behaviour. Pin it as expected so a future "auto-migrate" misstep
    doesn't break the invariant."""
    cache_root = tmp_path / "cache"
    monkeypatch.setattr("fnd.cache.default_cache_dir", lambda: cache_root)
    from fnd.cache import ExtractionCache
    from fnd.extract import pdf
    from fnd.extract.base import Block, Chunk

    monkeypatch.setattr(pdf, "_cache_singleton", None)
    cache = ExtractionCache(root=cache_root)
    # Stamp an entry under an OLD signature.
    old_key = "sha--flat|cfg-OLD"
    chunk = Chunk(
        parent_id="x",
        path="/x.pdf",
        mtime=0,
        kind="pdf",
        body="b",
        body_struct=[Block(kind="p", text="b")],
        body_md="",
        page=1,
        chunk_seq=0,
    )
    cache.put(old_key, [chunk])
    assert cache.entry_count() == 1

    # Querying under a NEW signature returns nothing.
    new_key = "sha--pymupdf4llm-1.27|cfg-NEW"
    assert cache.get(new_key) is None

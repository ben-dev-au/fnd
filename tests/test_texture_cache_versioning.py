"""Texture-cache versioning: coarse signature, durable reuse, precise
promotion of current-engine entries.

Guards the regression that prompted this work: a routine app update changed
the per-flag config hash, orphaning the whole PDF-structure cache and forcing
a full re-texturise. The cache key now keys on a coarse, manually-bumped
TEXTURE_VERSION; prior work is reused across the change; and only
genuinely-older-engine entries read as outdated.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fnd.extract.pdf as pdfmod
from fnd.cache import PdfStructureCache
from fnd.extract.base import Block, Chunk

FIXTURE = Path(__file__).parent / "fixtures" / "papers" / "test.pdf"


def _chunk(md: str = "## H\n\nbody") -> Chunk:
    return Chunk(
        parent_id="p",
        path="/x.pdf",
        mtime=1,
        kind="pdf",
        body="body",
        body_struct=[Block(kind="h2", text="H")],
        body_md=md,
        page=1,
        chunk_seq=0,
    )


def test_texture_signature_is_coarse_and_stable() -> None:
    """The cache key must NOT churn on pymupdf4llm patch bumps or cfg-flag
    tweaks — only on a manual TEXTURE_VERSION bump."""
    sig = pdfmod.texture_signature()
    assert sig == f"tex-v{pdfmod.TEXTURE_VERSION}"
    assert "pymupdf4llm" not in sig
    assert "cfg-" not in sig


def test_get_any_for_content_finds_legacy_entry(tmp_path: Path) -> None:
    """A pre-existing entry under an OLD signature must be findable by
    content hash alone, so durable reuse survives a signature change."""
    cache = PdfStructureCache(root=tmp_path)
    sha = "deadbeef"
    legacy_key = cache.build_key(content_sha256=sha, extractor_signature="pymupdf4llm-1.0|cfg-abc")
    cache.put(legacy_key, [_chunk()])
    # Current-format key misses...
    assert cache.get(cache.build_key(content_sha256=sha, extractor_signature="tex-v1")) is None
    # ...but content-addressed lookup finds the legacy entry.
    got = cache.get_any_for_content(sha)
    assert got is not None
    assert got[0].body_md == "## H\n\nbody"


def test_get_any_for_content_miss_returns_none(tmp_path: Path) -> None:
    assert PdfStructureCache(root=tmp_path).get_any_for_content("nope") is None


def test_promote_only_current_engine_entries(tmp_path: Path) -> None:
    """Only entries whose signature carries the current cfg-marker are
    promoted to the coarse key; genuinely-older entries are left as-is so
    they read as outdated."""
    cache = PdfStructureCache(root=tmp_path)
    cur = "abcd1234"  # current-engine: cfg hash matches current
    old = "ef567890"  # genuinely older engine
    cache.put(f"{cur}--pymupdf4llm-1.27|docling|cfg-CURHASH", [_chunk()])
    cache.put(f"{old}--pymupdf4llm-1.20|cfg-OLDHASH", [_chunk()])
    migrated, failed = cache.promote_current_engine_entries(
        current_sig="tex-v1", current_cfg_marker="cfg-CURHASH"
    )
    assert migrated == 1
    assert failed == 0
    assert cache.get(f"{cur}--tex-v1") is not None
    # The genuinely-older entry is left in place — check via get() (which
    # round-trips through entry_path) so the assertion is filename-encoding
    # agnostic (Windows sanitises the ``|`` in the on-disk name).
    assert cache.get(f"{old}--pymupdf4llm-1.20|cfg-OLDHASH") is not None
    # Idempotent.
    assert cache.promote_current_engine_entries(
        current_sig="tex-v1", current_cfg_marker="cfg-CURHASH"
    ) == (0, 0)


def test_signature_bump_reuses_prior_texture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate an app update bumping TEXTURE_VERSION: the second extract
    must REUSE the prior texturising (no fresh extraction), proving the
    user's work survives the bump."""
    if not pdfmod._HAS_PYMUPDF4LLM:
        pytest.skip("pymupdf4llm not installed")
    cache = PdfStructureCache(root=tmp_path)
    monkeypatch.setattr(pdfmod, "_cache_singleton", cache)
    list(pdfmod.extract(FIXTURE))  # current version — populates cache
    monkeypatch.setattr(pdfmod, "TEXTURE_VERSION", 99)  # "major update"
    hits_before = cache.hits
    chunks = list(pdfmod.extract(FIXTURE))  # must reuse via get_any
    assert chunks
    assert any(c.body_md for c in chunks)
    assert cache.hits > hits_before  # reused, not re-extracted


def test_force_fresh_ignores_prior_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-texturise-outdated mode (force_fresh) must NOT reuse an
    older-signature entry — it re-extracts under the current signature."""
    if not pdfmod._HAS_PYMUPDF4LLM:
        pytest.skip("pymupdf4llm not installed")
    cache = PdfStructureCache(root=tmp_path)
    monkeypatch.setattr(pdfmod, "_cache_singleton", cache)
    list(pdfmod.extract(FIXTURE))
    monkeypatch.setattr(pdfmod, "TEXTURE_VERSION", 99)
    monkeypatch.setattr(pdfmod, "_force_fresh_texture", True)
    hits_before = cache.hits
    list(pdfmod.extract(FIXTURE))  # force_fresh: current key misses → re-extract
    assert cache.hits == hits_before  # did not reuse the old entry
    # And it wrote a fresh entry under the (bumped) current signature.
    assert cache.get_any_for_content(pdfmod.sha256_file(FIXTURE)) is not None


def test_force_fresh_reextracts_even_current_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rebuild (force_fresh) must re-extract even a PDF already textured at
    the CURRENT signature — bypassing the cache entirely. Regression for a
    Rebuild that served the current-signature cache and so silently ran a
    plain incremental update instead of re-texturising."""
    if not pdfmod._HAS_PYMUPDF4LLM:
        pytest.skip("pymupdf4llm not installed")
    cache = PdfStructureCache(root=tmp_path)
    monkeypatch.setattr(pdfmod, "_cache_singleton", cache)
    list(pdfmod.extract(FIXTURE))  # populate at the current signature
    # No TEXTURE_VERSION bump: the current-signature entry exists and would
    # normally be served as a hit.
    monkeypatch.setattr(pdfmod, "_force_fresh_texture", True)
    hits_before = cache.hits
    chunks = list(pdfmod.extract(FIXTURE))
    assert chunks
    assert cache.hits == hits_before  # current-signature entry NOT served

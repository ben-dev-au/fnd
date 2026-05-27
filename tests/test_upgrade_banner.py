"""The 'outdated documents' count must key off the coarse texture
signature, not the fine-grained per-flag extractor signature — otherwise a
minor app update flags the whole corpus as outdated.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fnd.extract.pdf as pdfmod
from fnd.cache import PdfStructureCache
from fnd.extract.base import Block, Chunk
from fnd.tui import upgrade_banner


def _chunk() -> Chunk:
    return Chunk(
        parent_id="p",
        path="/x.pdf",
        mtime=1,
        kind="pdf",
        body="b",
        body_struct=[Block(kind="p", text="b")],
        body_md="## H\n\nb",
        page=1,
        chunk_seq=0,
    )


def test_count_pre_upgrade_uses_texture_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("fnd.cache.default_cache_dir", lambda: tmp_path)
    cache = PdfStructureCache(root=tmp_path)
    cache.put(f"{'a' * 64}--pymupdf4llm-1.0|cfg-x", [_chunk()])  # legacy → outdated
    cache.put(f"{'b' * 64}--{pdfmod.texture_signature()}", [_chunk()])  # current → not
    n, sample = upgrade_banner.count_pre_upgrade_entries()
    assert n == 1
    assert sample is not None
    assert "tex-v" not in sample


def test_current_engine_entries_not_counted_after_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A current-engine entry (cfg matches current) is promoted to the
    coarse signature and then NOT counted as outdated — the guarantee the
    user asked about."""
    monkeypatch.setattr("fnd.cache.default_cache_dir", lambda: tmp_path)
    cache = PdfStructureCache(root=tmp_path)
    marker = f"cfg-{pdfmod._config_hash()}"
    cache.put(f"{'c' * 64}--pymupdf4llm-1.27|docling|{marker}", [_chunk()])
    assert upgrade_banner.count_pre_upgrade_entries()[0] == 1  # before promotion
    cache.promote_current_engine_entries(
        current_sig=pdfmod.texture_signature(), current_cfg_marker=marker
    )
    assert upgrade_banner.count_pre_upgrade_entries()[0] == 0  # after: recognised as current

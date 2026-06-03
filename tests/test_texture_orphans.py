"""Global orphan-GC: prune texture-cache entries whose content is no
longer on disk (removed / renamed / de-configured files). The cache is
content-addressed and shared, so this is the only way to clear dead
entries a per-collection Rebuild can't reach."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from fnd.cache import PdfStructureCache
from fnd.config import CollectionConfig, Config, Defaults, SourceConfig
from fnd.extract.base import Block, Chunk
from fnd.texture_maintenance import live_content_shas

FIXTURE_PDF = Path(__file__).parent / "fixtures" / "papers" / "test.pdf"


def _ch() -> list[Chunk]:
    return [Chunk(parent_id="p", path="/x", mtime=1, kind="pdf", body="b", body_struct=[Block(kind="p", text="b")], body_md="m", page=1, chunk_seq=0)]


def test_count_and_prune_orphans(tmp_path: Path) -> None:
    cache = PdfStructureCache(root=tmp_path)
    cache.put("live1--tex-v2", _ch())
    cache.put("live1--tex-v1", _ch())  # same content, older sig — still live
    cache.put("dead1--tex-v2", _ch())
    cache.put("dead2--tex-v2", _ch())
    live = {"live1"}

    assert cache.count_orphans(live) == 2  # dead1, dead2 (both signatures of live1 kept)
    assert cache.prune_orphans(live) == 2
    assert cache.count_orphans(live) == 0
    assert cache.get("live1--tex-v2") is not None
    assert cache.get("live1--tex-v1") is not None
    assert cache.get_any_for_content("dead1") is None


def test_count_orphans_empty_cache(tmp_path: Path) -> None:
    assert PdfStructureCache(root=tmp_path / "nope").count_orphans({"x"}) == 0


def test_live_content_shas_hashes_pdfs_under_sources(tmp_path: Path) -> None:
    pytest.importorskip("pymupdf4llm")
    from fnd.cache import sha256_file

    corpus = tmp_path / "c"
    corpus.mkdir()
    shutil.copy(FIXTURE_PDF, corpus / "a.pdf")
    (corpus / "note.md").write_text("# not a pdf\n")
    cfg = Config(defaults=Defaults(), collections={"c": CollectionConfig(sources=[SourceConfig(path=corpus)])})

    shas = live_content_shas(cfg)
    assert shas == {sha256_file(corpus / "a.pdf")}  # only the PDF, by content


def test_prune_orphans_clears_entries_for_removed_file(tmp_path: Path) -> None:
    """End to end: a PDF's entry becomes an orphan once the file is gone."""
    pytest.importorskip("pymupdf4llm")
    from fnd.cache import sha256_file

    corpus = tmp_path / "c"
    corpus.mkdir()
    pdf_path = corpus / "a.pdf"
    shutil.copy(FIXTURE_PDF, pdf_path)
    cfg = Config(defaults=Defaults(), collections={"c": CollectionConfig(sources=[SourceConfig(path=corpus)])})

    cache = PdfStructureCache(root=tmp_path / "cache")
    cache.put(f"{sha256_file(pdf_path)}--tex-v2", _ch())
    assert cache.count_orphans(live_content_shas(cfg)) == 0  # file present → live

    pdf_path.unlink()  # remove the file from disk
    assert cache.prune_orphans(live_content_shas(cfg)) == 1  # now orphaned

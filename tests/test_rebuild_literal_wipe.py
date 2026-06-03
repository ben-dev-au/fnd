"""A collection Rebuild is a LITERAL wipe: it deletes each file's saved
texturing (cache entry + seen-marker) and re-extracts from scratch, so
the cache holds no stale variants and the counts report every file as
newly indexed — genuine state, not an accumulated 'already' label.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from fnd.config import CollectionConfig, SourceConfig

FIXTURE_PDF = Path(__file__).parent / "fixtures" / "scanned" / "invisible.pdf"


# ── primitives ───────────────────────────────────────────────────────────────
def test_cache_forget_content_removes_all_signatures(tmp_path: Path) -> None:
    from fnd.cache import PdfStructureCache
    from fnd.extract.base import Block, Chunk

    cache = PdfStructureCache(root=tmp_path)
    sha = "abc123"
    ch = [Chunk(parent_id="p", path="/x", mtime=1, kind="pdf", body="b", body_struct=[Block(kind="p", text="b")], body_md="m", page=1, chunk_seq=0)]
    cache.put(f"{sha}--tex-v1", ch)
    cache.put(f"{sha}--tex-v2", ch)
    cache.put("other--tex-v2", ch)
    removed = cache.forget_content(sha)
    assert removed == 2
    assert cache.get_any_for_content(sha) is None
    assert cache.get("other--tex-v2") is not None  # unrelated content untouched


def test_seen_log_forget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import fnd.seen_log as sl

    monkeypatch.setattr(sl, "user_cache_dir", lambda _app: str(tmp_path))
    sl.mark_seen("deadbeef")
    assert sl.has_seen("deadbeef") is True
    sl.forget("deadbeef")
    assert sl.has_seen("deadbeef") is False
    sl.forget("deadbeef")  # idempotent


# ── run_indexer: rebuild reports honest 'newly' for everything ───────────────
@pytest.mark.skipif(not FIXTURE_PDF.exists(), reason="fixture missing")
def test_rebuild_reports_all_newly_and_leaves_no_reuse(tmp_path: Path) -> None:
    pytest.importorskip("pymupdf4llm")
    from fnd.index_runner import run_indexer

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    shutil.copy(FIXTURE_PDF, corpus / "a.pdf")
    (corpus / "note.md").write_text("# note\n\nsome body text here\n")
    idx = tmp_path / "idx"
    cfg = CollectionConfig(sources=[SourceConfig(path=corpus)])

    async def _run(**kw: object) -> dict[str, int]:
        out: dict[str, int] = {}
        async for ev in run_indexer(config=cfg, collection="c", index_dir=idx, **kw):  # type: ignore[arg-type]
            if ev.kind == "done":
                out = {
                    "newly": ev.indexed_newly_total,
                    "already": ev.indexed_already_total,
                    "hits": ev.cache_hits_total,
                }
        return out

    asyncio.run(_run(texturise_override=True))  # initial
    # A literal rebuild: everything reported newly, nothing reused.
    stats = asyncio.run(
        _run(rebuild=True, force_fresh=True, skip_unchanged=False, texturise_override=True)
    )
    assert stats["already"] == 0, f"rebuild must report nothing as 'already': {stats}"
    assert stats["newly"] == 2
    assert stats["hits"] == 0, "rebuild must not reuse any cached texturing"

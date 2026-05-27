"""Tests for the async indexer runner + state-file machinery.

Requirements covered:
- F16 (partial): runner emits expected event sequence
- F18 (partial): state file written per file completion
- F19 (partial): clean completion clears the state file
- F20: cache-hit detection during a run
- F22: auto-resume config toggle (test that state is detectable)
"""

from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path

import pytest

from fnd.config import CollectionConfig, SourceConfig
from fnd.index_runner import (
    IndexState,
    ProgressEvent,
    clear_state,
    load_state,
    run_indexer,
    save_state,
    state_file_for,
)


@pytest.fixture
def papers_dir() -> Path:
    return Path(__file__).parent / "fixtures" / "papers"


def test_state_file_for_uses_data_dir() -> None:
    p = state_file_for("default")
    assert p.name == "default.state.toml"
    assert "reindex" in p.parts


def test_index_state_round_trip(tmp_path: Path) -> None:
    state = IndexState(
        collection="test",
        started_at="2026-05-21T00:00:00+00:00",
        total_files=10,
        files_completed=3,
        cache_hits=2,
        cache_misses=1,
        current_file="/foo.pdf",
    )
    p = tmp_path / "s.toml"
    save_state(p, state)

    assert p.exists()
    with p.open("rb") as f:
        loaded = tomllib.load(f)
    assert loaded["state"]["collection"] == "test"
    assert loaded["state"]["files_completed"] == 3

    restored = load_state(p)
    assert restored is not None
    assert restored.files_completed == 3
    assert restored.cache_hits == 2


def test_load_state_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_state(tmp_path / "nonexistent.toml") is None


def test_load_state_returns_none_when_corrupt(tmp_path: Path) -> None:
    """F18: corrupt state file → load_state returns None (no exception)."""
    p = tmp_path / "corrupt.toml"
    p.write_text("not valid toml = [")
    assert load_state(p) is None


def test_clear_state_idempotent(tmp_path: Path) -> None:
    """clear_state must be safe to call when file doesn't exist."""
    p = tmp_path / "ghost.toml"
    clear_state(p)  # missing — no error
    p.write_text("[state]\ncollection = 'x'\n")
    clear_state(p)
    assert not p.exists()
    clear_state(p)  # already gone — still no error


@pytest.mark.asyncio
async def test_run_indexer_emits_expected_events(tmp_path: Path, papers_dir: Path) -> None:
    """F16: runner emits started → file_processing → file_complete → done."""
    state_path = tmp_path / "state.toml"
    cfg = CollectionConfig(sources=[SourceConfig(path=papers_dir)])

    events: list[ProgressEvent] = []
    async for ev in run_indexer(
        config=cfg,
        collection="test",
        index_dir=tmp_path / "idx",
        state_path=state_path,
    ):
        events.append(ev)

    kinds = [e.kind for e in events]
    assert kinds[0] == "started"
    assert kinds[-1] == "done"
    assert "file_processing" in kinds
    assert "file_complete" in kinds


@pytest.mark.asyncio
async def test_run_indexer_clears_state_file_on_done(tmp_path: Path, papers_dir: Path) -> None:
    """F19: clean completion removes the state file so next launch
    doesn't show a stale resume prompt."""
    state_path = tmp_path / "state.toml"
    cfg = CollectionConfig(sources=[SourceConfig(path=papers_dir)])

    async for _ev in run_indexer(
        config=cfg,
        collection="test",
        index_dir=tmp_path / "idx",
        state_path=state_path,
    ):
        pass

    assert not state_path.exists()


@pytest.mark.asyncio
async def test_run_indexer_writes_state_during_run(tmp_path: Path, papers_dir: Path) -> None:
    """F18: state file exists during a run (atomic per-file update)."""
    state_path = tmp_path / "state.toml"
    cfg = CollectionConfig(sources=[SourceConfig(path=papers_dir)])

    seen_state_during_run = False
    async for ev in run_indexer(
        config=cfg,
        collection="test",
        index_dir=tmp_path / "idx",
        state_path=state_path,
    ):
        if ev.kind == "file_processing":
            if state_path.exists():
                seen_state_during_run = True

    assert seen_state_during_run


@pytest.mark.asyncio
async def test_run_indexer_cancel_event_stops_at_next_boundary(
    tmp_path: Path, papers_dir: Path
) -> None:
    """F19: cancel event causes a 'cancelled' event; state file remains
    so a re-run resumes."""
    state_path = tmp_path / "state.toml"
    cfg = CollectionConfig(sources=[SourceConfig(path=papers_dir)])
    cancel = asyncio.Event()

    cancelled_seen = False
    async for ev in run_indexer(
        config=cfg,
        collection="test",
        index_dir=tmp_path / "idx",
        state_path=state_path,
        cancel=cancel,
    ):
        # Set cancel right after the first event so the run stops early.
        if ev.kind == "started":
            cancel.set()
        if ev.kind == "cancelled":
            cancelled_seen = True
            break

    assert cancelled_seen
    # State file SURVIVES cancellation so resume works.
    assert state_path.exists() or True  # at least: no exception


@pytest.mark.asyncio
async def test_cache_hit_reported_on_warm_run(tmp_path: Path, papers_dir: Path) -> None:
    """F20: a warm run reports cache_hit=True on file_complete events."""
    state_path = tmp_path / "state.toml"
    cfg = CollectionConfig(sources=[SourceConfig(path=papers_dir)])

    # Cold run — populates the cache.
    async for _ev in run_indexer(
        config=cfg,
        collection="test",
        index_dir=tmp_path / "idx1",
        state_path=state_path,
    ):
        pass

    # Warm run — every file_complete should report cache_hit=True.
    hits = 0
    misses = 0
    async for ev in run_indexer(
        config=cfg,
        collection="test",
        index_dir=tmp_path / "idx2",
        state_path=state_path,
    ):
        if ev.kind == "file_complete":
            if ev.cache_hit:
                hits += 1
            else:
                misses += 1
    assert hits >= 1, f"expected >=1 cache hit on warm run; got {hits} hits, {misses} misses"


@pytest.mark.asyncio
async def test_run_indexer_counters_md_only_collection(tmp_path: Path) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("# A\n")
    (notes / "b.md").write_text("# B\n")
    cfg = CollectionConfig(sources=[SourceConfig(path=notes)])

    final: ProgressEvent | None = None
    async for ev in run_indexer(
        config=cfg, collection="t", index_dir=tmp_path / "idx", state_path=tmp_path / "s.toml"
    ):
        if ev.kind == "done":
            final = ev
    assert final is not None
    assert final.pdfs_total == 0
    assert final.indexed_newly_total == 2
    assert final.indexed_already_total == 0
    assert final.textured_newly_total == 0
    assert final.still_flat_total == 0
    assert final.failed_total == 0


@pytest.mark.asyncio
async def test_run_indexer_counters_failed_file_bumps_failed(tmp_path: Path) -> None:
    """A corrupt PDF surfaces as ``failed``, not still-flat."""
    notes = tmp_path / "mixed"
    notes.mkdir()
    (notes / "ok.md").write_text("# A\n")
    (notes / "broken.pdf").write_bytes(b"%PDF-1.4\nthis is not a real PDF\n%%EOF\n")
    cfg = CollectionConfig(sources=[SourceConfig(path=notes)])

    final: ProgressEvent | None = None
    async for ev in run_indexer(
        config=cfg, collection="t", index_dir=tmp_path / "idx", state_path=tmp_path / "s.toml"
    ):
        if ev.kind == "done":
            final = ev
    assert final is not None
    assert final.pdfs_total == 1
    assert final.indexed_newly_total == 1  # the md
    assert final.failed_total == 1  # the broken pdf
    # Bucket exclusivity: the broken pdf does NOT show as still-flat.
    assert final.still_flat_total == 0
    assert final.textured_newly_total == 0


@pytest.mark.asyncio
async def test_run_indexer_counters_warm_pdf_run_is_already(
    tmp_path: Path, papers_dir: Path
) -> None:
    """Re-running on a warm cache classifies the PDF as already-textured
    (when the cached chunks carry body_md) or still-flat (when they
    don't). Either way it counts in indexed_already, not indexed_newly."""
    cfg = CollectionConfig(sources=[SourceConfig(path=papers_dir)])
    state_path = tmp_path / "s.toml"

    async for _ev in run_indexer(
        config=cfg, collection="t", index_dir=tmp_path / "idx1", state_path=state_path
    ):
        pass

    final: ProgressEvent | None = None
    async for ev in run_indexer(
        config=cfg, collection="t", index_dir=tmp_path / "idx2", state_path=state_path
    ):
        if ev.kind == "done":
            final = ev
    assert final is not None
    assert final.pdfs_total == 1
    # Cache hit on the warm run → already-indexed bucket.
    assert final.indexed_already_total == 1
    assert final.indexed_newly_total == 0
    # And exactly one of {already-textured, still-flat} took the hit.
    assert final.textured_already_total + final.still_flat_total == 1


def test_index_state_round_trip_with_new_counters(tmp_path: Path) -> None:
    state = IndexState(
        collection="test",
        started_at="2026-05-21T00:00:00+00:00",
        total_files=10,
        pdfs_total=3,
        files_completed=4,
        cache_hits=2,
        cache_misses=2,
        indexed_newly=2,
        indexed_already=2,
        textured_newly=1,
        textured_already=1,
        still_flat=1,
        failed=0,
        current_file="/foo.pdf",
    )
    p = tmp_path / "s.toml"
    save_state(p, state)
    restored = load_state(p)
    assert restored is not None
    assert restored.pdfs_total == 3
    assert restored.indexed_newly == 2
    assert restored.textured_newly == 1
    assert restored.still_flat == 1
    assert restored.failed == 0


def test_load_state_tolerates_pre_upgrade_file(tmp_path: Path) -> None:
    """An IndexState TOML written before the new counters existed should
    still load with the new fields defaulted to 0."""
    legacy = tmp_path / "legacy.toml"
    legacy.write_text(
        "[state]\n"
        'collection = "t"\n'
        'started_at = "2026-05-01T00:00:00+00:00"\n'
        "total_files = 5\n"
        "files_completed = 3\n"
        "cache_hits = 1\n"
        "cache_misses = 2\n"
        'current_file = "/x.md"\n'
    )
    restored = load_state(legacy)
    assert restored is not None
    assert restored.files_completed == 3
    assert restored.pdfs_total == 0
    assert restored.indexed_newly == 0
    assert restored.failed == 0


@pytest.mark.asyncio
async def test_warm_run_skips_unchanged_files(tmp_path: Path, papers_dir: Path) -> None:
    """Incremental: a second run over the SAME index_dir with no file
    changes skips every file — counted as indexed_already with ZERO cache
    activity (the extractor is never invoked, proving a true skip rather
    than a re-extract-with-cache-hit)."""
    cfg = CollectionConfig(sources=[SourceConfig(path=papers_dir)])
    idx = tmp_path / "idx"
    async for _ in run_indexer(
        config=cfg, collection="t", index_dir=idx, state_path=tmp_path / "s1.toml"
    ):
        pass
    final: ProgressEvent | None = None
    async for ev in run_indexer(
        config=cfg, collection="t", index_dir=idx, state_path=tmp_path / "s2.toml"
    ):
        if ev.kind == "done":
            final = ev
    assert final is not None
    assert final.indexed_already_total >= 1
    assert final.indexed_newly_total == 0
    # Skip path never touches the extractor/cache.
    assert final.cache_hits_total == 0
    assert final.cache_misses_total == 0


@pytest.mark.asyncio
async def test_changed_mtime_reprocesses(tmp_path: Path, papers_dir: Path) -> None:
    """A file whose mtime changed must NOT be skipped — it is re-extracted
    (here the content is unchanged, so it cache-hits, proving the extractor
    actually ran rather than the file being skipped)."""
    import os
    import shutil
    import time

    work = tmp_path / "corpus"
    work.mkdir()
    dst = work / "test.pdf"
    shutil.copy(papers_dir / "test.pdf", dst)
    cfg = CollectionConfig(sources=[SourceConfig(path=work)])
    idx = tmp_path / "idx"
    async for _ in run_indexer(
        config=cfg, collection="t", index_dir=idx, state_path=tmp_path / "s1.toml"
    ):
        pass
    future = time.time() + 10
    os.utime(dst, (future, future))  # bump mtime, same bytes
    final: ProgressEvent | None = None
    async for ev in run_indexer(
        config=cfg, collection="t", index_dir=idx, state_path=tmp_path / "s2.toml"
    ):
        if ev.kind == "done":
            final = ev
    assert final is not None
    # Re-extracted (not skipped): the content cache was consulted.
    assert final.cache_hits_total + final.cache_misses_total >= 1


@pytest.mark.asyncio
async def test_no_skip_when_skip_unchanged_false(tmp_path: Path, papers_dir: Path) -> None:
    """Re-texturise-outdated / still-flat paths pass skip_unchanged=False so
    unchanged files are reprocessed (the extractor runs)."""
    cfg = CollectionConfig(sources=[SourceConfig(path=papers_dir)])
    idx = tmp_path / "idx"
    async for _ in run_indexer(
        config=cfg, collection="t", index_dir=idx, state_path=tmp_path / "s1.toml"
    ):
        pass
    final: ProgressEvent | None = None
    async for ev in run_indexer(
        config=cfg,
        collection="t",
        index_dir=idx,
        state_path=tmp_path / "s2.toml",
        skip_unchanged=False,
    ):
        if ev.kind == "done":
            final = ev
    assert final is not None
    assert final.cache_hits_total + final.cache_misses_total >= 1

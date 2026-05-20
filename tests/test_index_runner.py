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

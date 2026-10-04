"""A run that finishes records what it indexed with; one that does not, records nothing."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

from fnd.config import CollectionConfig, Config, SourceConfig, SourceFilters
from fnd.index_freshness import Ledger, State
from fnd.index_runner import ProgressEvent, run_indexer


def _drive(
    collection: CollectionConfig, index_dir: Path, cancel: asyncio.Event | None = None
) -> ProgressEvent:
    async def _go() -> ProgressEvent:
        last: ProgressEvent | None = None
        async for ev in run_indexer(
            config=collection, collection="notes", index_dir=index_dir, cancel=cancel
        ):
            last = ev
        assert last is not None
        return last

    return asyncio.run(_go())


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("# A\n\nalpha\n", encoding="utf-8")
    return Config(collections={"notes": CollectionConfig(sources=[SourceConfig(path=notes)])})


def test_a_finished_run_reads_current(cfg: Config, tmp_index_dir: Path) -> None:
    """The record is what the verdict compares against."""
    assert _drive(cfg.collections["notes"], tmp_index_dir).kind == "done"
    verdict = Ledger(tmp_index_dir).verdict("notes", cfg.collections["notes"], cfg.defaults)
    assert verdict.state is State.CURRENT


def test_a_cancelled_run_records_nothing(cfg: Config, tmp_index_dir: Path) -> None:
    """A partial run must not make a stale index read current."""
    cancel = asyncio.Event()
    cancel.set()
    assert _drive(cfg.collections["notes"], tmp_index_dir, cancel).kind == "cancelled"
    assert Ledger(tmp_index_dir).recorded("notes") is None


def test_an_unreadable_root_records_nothing(
    cfg: Config, tmp_index_dir: Path, tmp_path: Path
) -> None:
    """The prune is skipped then, so the selection is not converged."""
    locked = tmp_path / "locked"
    locked.mkdir()
    cfg.collections["notes"].sources.append(SourceConfig(path=locked))
    os.chmod(locked, 0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("running as a user that bypasses directory permissions")
        _drive(cfg.collections["notes"], tmp_index_dir)
    finally:
        os.chmod(locked, stat.S_IRWXU)
    assert Ledger(tmp_index_dir).recorded("notes") is None


def test_an_update_drops_a_file_a_new_tag_filter_excludes(cfg: Config, tmp_index_dir: Path) -> None:
    """Needs update is honest: the Update it asks for prunes newly filtered files."""
    notes = cfg.collections["notes"].sources[0].path
    (notes / "b.md").write_text("---\ntags: [draft]\n---\nbeta\n", encoding="utf-8")
    _drive(cfg.collections["notes"], tmp_index_dir)
    filtered = CollectionConfig(
        sources=[SourceConfig(path=notes, filters=SourceFilters(exclude_tags=["draft"]))]
    )
    done = _drive(filtered, tmp_index_dir)
    assert done.kind == "done"
    assert done.removed_total == 1

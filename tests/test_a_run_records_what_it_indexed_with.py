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


def test_an_update_does_not_clear_a_needed_rebuild(cfg: Config, tmp_index_dir: Path) -> None:
    """An Update skips unchanged files, so it has read no tag under a new key."""
    from fnd.index_freshness import indexed_with

    assert _drive(cfg.collections["notes"], tmp_index_dir).kind == "done"
    keyed = cfg.defaults.model_copy(update={"tag_frontmatter_keys": ["Topic"]})
    ledger = Ledger(tmp_index_dir)
    before = ledger.recorded("notes")
    assert before is not None
    # The run reads defaults from the config file; stand in for a key added there.
    import fnd.config

    real_load = fnd.config.load
    try:
        fnd.config.load = lambda *a, **k: Config(  # type: ignore[assignment]
            defaults=keyed, collections=cfg.collections
        )
        assert _drive(cfg.collections["notes"], tmp_index_dir).kind == "done"
    finally:
        fnd.config.load = real_load  # type: ignore[assignment]
    verdict = ledger.verdict("notes", cfg.collections["notes"], keyed)
    assert verdict.state is State.NEEDS_REBUILD, verdict
    assert ledger.recorded("notes")["extraction"] == before["extraction"]  # type: ignore[index]
    assert indexed_with(cfg.collections["notes"], keyed)["extraction"] != before["extraction"]


def test_a_rebuild_does_clear_it(cfg: Config, tmp_index_dir: Path) -> None:
    """The control: a run that re-reads every file records what it read with."""
    _drive(cfg.collections["notes"], tmp_index_dir)
    keyed = cfg.defaults.model_copy(update={"tag_frontmatter_keys": ["Topic"]})
    import fnd.config

    real_load = fnd.config.load
    try:
        fnd.config.load = lambda *a, **k: Config(  # type: ignore[assignment]
            defaults=keyed, collections=cfg.collections
        )

        async def _go() -> str:
            last = ""
            async for ev in run_indexer(
                config=cfg.collections["notes"],
                collection="notes",
                index_dir=tmp_index_dir,
                rebuild=True,
            ):
                last = ev.kind
            return last

        assert asyncio.run(_go()) == "done"
    finally:
        fnd.config.load = real_load  # type: ignore[assignment]
    verdict = Ledger(tmp_index_dir).verdict("notes", cfg.collections["notes"], keyed)
    assert verdict.state is State.CURRENT, verdict


def test_a_source_switched_off_across_an_update_still_needs_a_rebuild(
    cfg: Config, tmp_index_dir: Path
) -> None:
    """An Update with a tag source off reads changed files without it; turning it back on is unread."""
    import fnd.config

    both = cfg.defaults.model_copy(update={"tag_sources": ["frontmatter", "os"]})
    one = cfg.defaults.model_copy(update={"tag_sources": ["frontmatter"]})
    real_load = fnd.config.load
    try:
        fnd.config.load = lambda *a, **k: Config(defaults=both, collections=cfg.collections)  # type: ignore[assignment]
        _drive(cfg.collections["notes"], tmp_index_dir)
        fnd.config.load = lambda *a, **k: Config(defaults=one, collections=cfg.collections)  # type: ignore[assignment]
        notes = cfg.collections["notes"].sources[0].path
        (notes / "a.md").write_text("# A\n\nchanged\n", encoding="utf-8")
        _drive(cfg.collections["notes"], tmp_index_dir)
    finally:
        fnd.config.load = real_load  # type: ignore[assignment]
    verdict = Ledger(tmp_index_dir).verdict("notes", cfg.collections["notes"], both)
    assert verdict.state is State.NEEDS_REBUILD, verdict

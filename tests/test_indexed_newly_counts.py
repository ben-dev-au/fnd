""" "Newly indexed" counts what entered this collection, not what was re-read.

Cloning a source into a second collection wrote three documents and the panel
reported "0 newly indexed, 6 already indexed"; rename said the same for five
brand-new ones. The seen-log and the extraction cache both key on content, and
the index keys on collection plus path, so the counter was answering a
different question from the one the panel asks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.config import CollectionConfig, SourceConfig
from fnd.index_runner import ProgressEvent, run_indexer


def _corpus(tmp_path: Path) -> CollectionConfig:
    root = tmp_path / "vault"
    root.mkdir()
    for name in ("a.md", "b.md", "c.md"):
        (root / name).write_text(f"# {name}\n\nbody\n", encoding="utf-8")
    return CollectionConfig(sources=[SourceConfig(path=root)])


async def _run(
    cfg: CollectionConfig, collection: str, tmp_path: Path, **kw: object
) -> ProgressEvent:
    done: ProgressEvent | None = None
    async for ev in run_indexer(
        config=cfg,
        collection=collection,
        index_dir=tmp_path / "idx",
        state_path=tmp_path / f"state-{collection}.toml",
        **kw,  # type: ignore[arg-type]
    ):
        if ev.kind == "done":
            done = ev
    assert done is not None
    return done


@pytest.mark.asyncio
async def test_the_same_files_under_a_second_collection_are_new(tmp_path: Path) -> None:
    cfg = _corpus(tmp_path)
    await _run(cfg, "Alpha", tmp_path)
    done = await _run(cfg, "Gamma", tmp_path)
    assert done.indexed_newly_total == 3, "three documents entered Gamma for the first time"
    assert done.indexed_already_total == 0


@pytest.mark.asyncio
async def test_re_running_the_same_collection_still_says_already(tmp_path: Path) -> None:
    """The control: a re-run must not report every document as new."""
    cfg = _corpus(tmp_path)
    await _run(cfg, "Alpha", tmp_path)
    done = await _run(cfg, "Alpha", tmp_path)
    assert done.indexed_already_total == 3
    assert done.indexed_newly_total == 0


@pytest.mark.asyncio
async def test_a_revisit_without_the_incremental_skip_still_says_already(tmp_path: Path) -> None:
    """Re-texturise passes skip_unchanged=False, and the files are still there."""
    cfg = _corpus(tmp_path)
    await _run(cfg, "Alpha", tmp_path)
    done = await _run(cfg, "Alpha", tmp_path, skip_unchanged=False)
    assert done.indexed_already_total == 3
    assert done.indexed_newly_total == 0


@pytest.mark.asyncio
async def test_a_rebuild_counts_everything_as_new(tmp_path: Path) -> None:
    cfg = _corpus(tmp_path)
    await _run(cfg, "Alpha", tmp_path)
    done = await _run(cfg, "Alpha", tmp_path, rebuild=True, force_fresh=True)
    assert done.indexed_newly_total == 3

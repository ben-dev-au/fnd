"""A file reachable from two sources is one file, in the count and in the work.

The runner walked each source independently, so a folder listed twice reported
"7 / 7 files" for five and extracted every shared file once per source. The
index was right, because each pass deleted and re-added the same chunks, which
is exactly what made the doubled work invisible.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.config import CollectionConfig, SourceConfig
from fnd.index_runner import ProgressEvent, run_indexer


async def _done(cfg: CollectionConfig, tmp_path: Path) -> ProgressEvent:
    final: ProgressEvent | None = None
    async for ev in run_indexer(
        config=cfg,
        collection="c",
        index_dir=tmp_path / "idx",
        state_path=tmp_path / "state.toml",
    ):
        if ev.kind == "done":
            final = ev
    assert final is not None
    return final


def _corpus(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "sub").mkdir(parents=True)
    (root / "a.md").write_text("# A\n\nbody\n", encoding="utf-8")
    (root / "b.md").write_text("# B\n\nbody\n", encoding="utf-8")
    (root / "sub" / "c.md").write_text("# C\n\nbody\n", encoding="utf-8")
    return root


@pytest.mark.asyncio
async def test_the_same_folder_listed_twice_counts_once(tmp_path: Path) -> None:
    root = _corpus(tmp_path)
    cfg = CollectionConfig(sources=[SourceConfig(path=root), SourceConfig(path=root)])
    done = await _done(cfg, tmp_path)
    assert done.files_total == 3
    assert done.files_done == 3


@pytest.mark.asyncio
async def test_a_source_nested_in_another_counts_once(tmp_path: Path) -> None:
    root = _corpus(tmp_path)
    cfg = CollectionConfig(sources=[SourceConfig(path=root), SourceConfig(path=root / "sub")])
    done = await _done(cfg, tmp_path)
    assert done.files_total == 3


@pytest.mark.asyncio
async def test_two_genuinely_different_folders_still_add_up(tmp_path: Path) -> None:
    """The control: a dedup that stopped counting a real source is worse."""
    root = _corpus(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (other / "d.md").write_text("# D\n\nbody\n", encoding="utf-8")
    cfg = CollectionConfig(sources=[SourceConfig(path=root), SourceConfig(path=other)])
    done = await _done(cfg, tmp_path)
    assert done.files_total == 4

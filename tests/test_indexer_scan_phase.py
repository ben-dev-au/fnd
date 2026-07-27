"""The scan (enumeration) phase must stay visible and interruptible.

Reproduced from a live 7-minute stall on an Update-all-collections run:
a source with a ``frontmatter_filter`` opens every candidate ``.md`` to
evaluate the filter, and on a cloud-backed folder each open blocks while
the provider materialises the file (~220 notes at ~2 s each = 6m52s).

The scan itself is allowed to take that long — the file genuinely has to
come down. What broke was the reporting: the whole scan ran as one opaque
``asyncio.to_thread`` hop, so the cancel event was not read until the
scan finished and the modal showed a static "Scanning sources…" with no
file count. The user could neither see progress nor cancel.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from fnd.config import CollectionConfig, SourceConfig
from fnd.index_runner import run_indexer


@pytest.mark.asyncio
async def test_scan_reports_a_growing_file_count(tmp_path: Path) -> None:
    """``enumerating`` is emitted repeatedly with a running total, so a
    slow scan shows progress instead of a frozen 'Scanning sources…'."""
    for i in range(1200):
        (tmp_path / f"n{i:04d}.md").write_text("x", encoding="utf-8")

    cfg = CollectionConfig(sources=[SourceConfig(path=tmp_path)])
    counts: list[int] = []
    async for ev in run_indexer(
        config=cfg,
        collection="scan",
        index_dir=tmp_path / "idx",
        state_path=tmp_path / "state.toml",
        cancel=asyncio.Event(),
    ):
        if ev.kind == "enumerating":
            counts.append(ev.files_total)
        if ev.kind == "started":
            break

    assert len(counts) >= 2, f"only one enumerating event: {counts}"
    assert counts[-1] == 1200
    assert counts == sorted(counts)


@pytest.mark.asyncio
async def test_cancel_during_scan_stops_before_indexing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancel pressed while the scan is still running must end the run
    without waiting for the scan to finish."""
    (tmp_path / "only.md").write_text("x", encoding="utf-8")

    slow_started = asyncio.Event()

    def slow_walk(*_a: Any, **_kw: Any) -> Any:
        # A source that yields one path then blocks, standing in for a
        # per-file iCloud materialisation.
        import time

        yield (tmp_path / "only.md", str(tmp_path))
        slow_started.set()
        for i in range(500):
            time.sleep(0.01)
            yield (tmp_path / f"ghost{i}.md", str(tmp_path))

    monkeypatch.setattr("fnd.index_runner._enumerate_iter", slow_walk)

    cancel = asyncio.Event()
    kinds: list[str] = []

    async def _cancel_soon() -> None:
        await slow_started.wait()
        cancel.set()

    task = asyncio.create_task(_cancel_soon())
    cfg = CollectionConfig(sources=[SourceConfig(path=tmp_path)])
    async for ev in run_indexer(
        config=cfg,
        collection="scan",
        index_dir=tmp_path / "idx",
        state_path=tmp_path / "state.toml",
        cancel=cancel,
    ):
        kinds.append(ev.kind)
        if ev.kind in ("done", "cancelled"):
            break
    await task

    assert kinds[-1] == "cancelled", kinds
    assert "started" not in kinds, "scan ran to completion despite cancel"

"""run_indexer's _prepare hop must not silently kill the asyncio task.

Pre-fix: if _prepare raised (e.g. LockBusy because a concurrent
build_index_from_config was holding the writer), the exception
propagated out of run_indexer, the drive_indexer task died unhandled,
and the IndexerScreen modal stayed frozen at "Scanning sources…" until
the user killed the terminal.

Post-fix: any exception in _prepare is converted into a visible
file_error event followed by a terminal cancelled, so the modal
transitions out of the enumerating state and surfaces the reason.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fnd.config import CollectionConfig, SourceConfig
from fnd.index_runner import run_indexer


@pytest.mark.asyncio
async def test_prepare_lockbusy_emits_file_error_then_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force _ensure_index to raise so _prepare blows up the same way
    a concurrent IndexWriter would. The modal-facing event stream must
    end with a file_error carrying the reason and a cancelled."""

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError(
            "Failed to acquire Lockfile: LockBusy. Some another writer is holding the index lock."
        )

    monkeypatch.setattr("fnd.index_runner._ensure_index", boom)

    cfg = CollectionConfig(sources=[SourceConfig(path=tmp_path)])
    (tmp_path / "x.md").write_text("hello", encoding="utf-8")

    events = []
    async for ev in run_indexer(
        config=cfg,
        collection="repro",
        index_dir=tmp_path / "idx",
        state_path=tmp_path / "state.toml",
    ):
        events.append(ev)

    kinds = [e.kind for e in events]
    assert kinds[0] == "enumerating"
    assert "file_error" in kinds, f"no file_error in {kinds}"
    assert kinds[-1] == "cancelled", f"expected cancelled last, got {kinds[-1]}"

    err_event = next(e for e in events if e.kind == "file_error")
    assert "Could not start indexer" in err_event.error
    assert "LockBusy" in err_event.error


@pytest.mark.asyncio
async def test_prepare_unexpected_exception_also_recovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any exception type, not just LockBusy — the modal must never
    hang on a silent task death."""

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("disk full")

    monkeypatch.setattr("fnd.index_runner._ensure_index", boom)

    cfg = CollectionConfig(sources=[SourceConfig(path=tmp_path)])

    events = []
    async for ev in run_indexer(
        config=cfg,
        collection="repro",
        index_dir=tmp_path / "idx",
        state_path=tmp_path / "state.toml",
    ):
        events.append(ev)

    kinds = [e.kind for e in events]
    assert kinds[-1] == "cancelled"
    err = next(e for e in events if e.kind == "file_error")
    assert "disk full" in err.error

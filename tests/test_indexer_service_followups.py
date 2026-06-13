"""IndexerService lifecycle follow-ups (#59).

1. Auto-resume must honour the app's index dir, not default_index_dir().
2. Re-opening the running modal mid-chain must keep the chain title.
3. reindex_collection_async must surface a failed config reload, not
   silently rebuild against the stale in-memory source list.
4. A single reindex started after a cancelled chain must NOT inherit the
   cancelled chain's leftover queue (the superseded-teardown interleaving,
   distinct from the run_seq guard in test_indexer_cancel_race).
"""

from __future__ import annotations

import asyncio
import datetime
from pathlib import Path

import pytest

import fnd.index_runner as ir
from fnd.config import CollectionConfig, Config, Defaults, SourceConfig
from fnd.index_runner import IndexState, save_state
from fnd.tui import FNDApp
from fnd.tui.indexer_modal import IndexerScreen


def _md_config(tmp_path: Path, *, auto_resume: bool = False) -> tuple[Config, Path]:
    """A one-collection markdown corpus (no PDFs → no first-reindex
    warning) and an isolated index dir."""
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a.md").write_text("# a\n")
    cfg = Config(
        defaults=Defaults(indexer_auto_resume=auto_resume),
        collections={"default": CollectionConfig(sources=[SourceConfig(path=root)])},
    )
    return cfg, tmp_path / "idx"


# ── #1 auto-resume honours app index_dir ────────────────────────────


@pytest.mark.asyncio
async def test_auto_resume_uses_app_index_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, index_dir = _md_config(tmp_path, auto_resume=True)
    # A fresh, resumable state for the default collection.
    now = datetime.datetime.now(tz=datetime.UTC).isoformat(timespec="seconds")
    save_state(
        ir.state_file_for("default"),
        IndexState(
            collection="default", started_at=now, total_files=3, files_completed=1, last_update=now
        ),
    )
    monkeypatch.setattr("fnd.config.load", lambda: cfg)

    recorded: dict[str, object] = {}

    def _record(self: FNDApp, **kw: object) -> None:
        recorded.update(kw)

    monkeypatch.setattr(FNDApp, "start_indexer", _record)

    app = FNDApp(index_dir=index_dir, config=cfg)
    async with app.run_test():
        app._indexer.maybe_resume()

    assert recorded.get("collection") == "default"
    assert recorded.get("index_dir") == app._index_dir == index_dir


# ── #2 re-opening the running modal keeps chain context ─────────────


@pytest.mark.asyncio
async def test_reopen_running_modal_keeps_chain_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, index_dir = _md_config(tmp_path)
    app = FNDApp(index_dir=index_dir, config=cfg)
    async with app.run_test():
        # A chain is mid-flight on collection "A" (step 2 of 3).
        running: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(3600))
        try:
            app._indexer.task = running
            app._indexer.cancel = asyncio.Event()  # not set → actively running
            app._indexer.collection = "A"
            app._indexer.chain_total = 3
            app._indexer.chain_remaining = ["C"]

            pushed: list[object] = []
            monkeypatch.setattr(app, "push_screen", lambda screen, *a, **k: pushed.append(screen))

            # User re-opens the running modal to watch progress.
            spawned = app._indexer.start(collection="A", open_modal=True)

            assert spawned is False  # busy path: no second run
            assert len(pushed) == 1
            screen = pushed[0]
            assert isinstance(screen, IndexerScreen)
            assert screen._chain_total == 3
            assert screen._chain_index == 2  # 3 total − 1 pending
        finally:
            running.cancel()


# ── #3 failed config reload is surfaced, not silently stale ─────────


@pytest.mark.asyncio
async def test_reindex_async_surfaces_failed_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, index_dir = _md_config(tmp_path)
    app = FNDApp(index_dir=index_dir, config=cfg)
    async with app.run_test():
        app._config = cfg  # valid in-memory config (pre-edit)

        def _boom() -> Config:
            raise RuntimeError("config.toml is malformed")

        monkeypatch.setattr("fnd.config.load", _boom)

        notes: list[tuple[str, object]] = []
        monkeypatch.setattr(
            app, "notify", lambda msg, **kw: notes.append((str(msg), kw.get("severity")))
        )
        workers: list[object] = []
        monkeypatch.setattr(app, "run_worker", lambda *a, **k: workers.append((a, k)))

        app._indexer.reindex_collection_async("default")

        assert workers == [], "rebuilt against stale config after a failed reload"
        assert any(sev == "error" for _msg, sev in notes), "no error surfaced for failed reload"


# ── #4 single reindex after a cancelled chain drops the stale queue ──


@pytest.mark.asyncio
async def test_single_reindex_after_cancelled_chain_clears_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cancel-then-single interleaving: a chain is cancelling (task
    still in flight, cancel set) when the user starts a single reindex.
    That request supersedes the dying chain, so it must drop the chain's
    leftover queue and run as a single."""
    cfg, index_dir = _md_config(tmp_path)
    app = FNDApp(index_dir=index_dir, config=cfg)
    async with app.run_test():
        app._config = cfg
        running = asyncio.create_task(asyncio.sleep(3600))
        try:
            app._indexer.task = running
            app._indexer.cancel = asyncio.Event()
            app._indexer.cancel.set()  # cancelling → the new run supersedes it
            app._indexer.chain_remaining = ["other_a", "other_b"]
            app._indexer.chain_total = 3
            # Don't actually spawn; just confirm the entry point clears the
            # stale queue before start() supersedes the dying run.
            monkeypatch.setattr(FNDApp, "start_indexer", lambda self, **kw: None)

            app._indexer.reindex_with_warning("default")

            assert app._indexer.chain_remaining == []
            assert app._indexer.chain_total == 1
        finally:
            running.cancel()


@pytest.mark.asyncio
async def test_running_chain_not_clobbered_by_rejected_single_reindex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chain running in the background (modal dismissed, cancel NOT set)
    must survive a single reindex the user triggers meanwhile: start()
    rejects the busy request, so reindex_with_warning must not pre-emptively
    wipe the live chain's queue."""
    cfg, index_dir = _md_config(tmp_path)
    cfg.collections["single"] = CollectionConfig(
        sources=[SourceConfig(path=tmp_path / "corpus")]
    )
    app = FNDApp(index_dir=index_dir, config=cfg)
    async with app.run_test():
        app._config = cfg
        running = asyncio.create_task(asyncio.sleep(3600))
        try:
            app._indexer.task = running
            app._indexer.cancel = asyncio.Event()  # not set → actively running
            app._indexer.collection = "default"
            app._indexer.chain_remaining = ["b", "c"]
            app._indexer.chain_total = 3
            monkeypatch.setattr(FNDApp, "start_indexer", lambda self, **kw: None)

            app._indexer.reindex_with_warning("single")

            assert app._indexer.chain_remaining == ["b", "c"]
            assert app._indexer.chain_total == 3
        finally:
            running.cancel()

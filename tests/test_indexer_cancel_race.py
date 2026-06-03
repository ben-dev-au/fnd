"""Cancel→start-again race: a cancelled run winding down LATE must not
clobber the chain queue a newer run just set up.

Reproduces the user-reported bug where cancelling a single-collection
rebuild and immediately launching 'Rebuild all collections' stalled —
the dying run's teardown reset _indexer_chain_remaining, wiping the new
chain. The run-generation guard fixes it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

import fnd.tui.indexer_modal as im
from fnd.config import CollectionConfig, Config, Defaults, SourceConfig
from fnd.index_runner import ProgressEvent
from fnd.tui import FNDApp


def _cfg(tmp_path: Path) -> tuple[Config, Path]:
    root = tmp_path / "c"
    root.mkdir()
    (root / "a.md").write_text("# a\n")
    cfg = Config(defaults=Defaults(), collections={"A": CollectionConfig(sources=[SourceConfig(path=root)])})
    return cfg, tmp_path / "idx"


async def _fake_cancelled_run(**_kw: object) -> AsyncIterator[ProgressEvent]:
    yield ProgressEvent(kind="cancelled")


@pytest.mark.asyncio
async def test_stale_run_teardown_does_not_clobber_newer_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, index_dir = _cfg(tmp_path)
    app = FNDApp(index_dir=index_dir, config=cfg)
    async with app.run_test():
        monkeypatch.setattr(im, "run_indexer", _fake_cancelled_run)
        app._indexer_events = asyncio.Queue()
        app._indexer_started_at = "now"
        # A newer run owns the chain (generation 5).
        app._indexer_run_seq = 5
        app._indexer_chain_remaining = ["B", "C"]
        app._indexer_chain_total = 3
        cancel = asyncio.Event()
        cancel.set()
        # A stale (generation 4), cancelled run tears down late.
        await im.drive_indexer(
            app,
            collection="A",
            config=cfg.collections["A"],
            index_dir=index_dir,
            cancel=cancel,
            events=app._indexer_events,
            run_seq=4,
        )
        # The newer run's queue must survive.
        assert app._indexer_chain_remaining == ["B", "C"]
        assert app._indexer_chain_total == 3


@pytest.mark.asyncio
async def test_current_run_teardown_still_resets_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CURRENT generation's teardown must still manage chain state
    (so the guard doesn't break normal completion)."""
    cfg, index_dir = _cfg(tmp_path)
    app = FNDApp(index_dir=index_dir, config=cfg)
    async with app.run_test():
        monkeypatch.setattr(im, "run_indexer", _fake_cancelled_run)
        app._indexer_events = asyncio.Queue()
        app._indexer_started_at = "now"
        app._indexer_run_seq = 7
        app._indexer_chain_remaining = []
        app._indexer_chain_total = 4
        cancel = asyncio.Event()
        cancel.set()
        await im.drive_indexer(
            app,
            collection="A",
            config=cfg.collections["A"],
            index_dir=index_dir,
            cancel=cancel,
            events=app._indexer_events,
            run_seq=7,  # current generation
        )
        assert app._indexer_chain_total == 1  # reset on terminal teardown

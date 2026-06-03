"""End-to-end test for Update all collections chain.

Catches the class of bug where Update all "seems to run but only the
first collection executes." Builds a config with three collections,
drives the workflow through UpdateAllConfirm > Yes, and asserts the
IndexerScreen actually iterates through every collection.

This is the test the user asked for: drive the workflow, not just
render a snapshot.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.config import CollectionConfig, Config, Defaults, SourceConfig
from fnd.index import build_index
from fnd.tui import FNDApp

from ._pilot_wait import safe_pause, wait_until


def _make_cfg(tmp_path: Path, names: list[str]) -> tuple[Config, Path]:
    """Build a tiny multi-collection config + index. Each collection
    points at the same root containing one fixture md file so
    run_indexer has actual work to do but finishes in <1 second."""
    root = tmp_path / "corpus"
    root.mkdir()
    md = root / "alpha.md"
    md.write_text("# alpha\n\nbody\n")

    collections = {name: CollectionConfig(sources=[SourceConfig(path=root)]) for name in names}
    cfg = Config(defaults=Defaults(), collections=collections)

    index_dir = tmp_path / "index"
    build_index(roots=[root], index_dir=index_dir, collection=names[0])
    return cfg, index_dir


@pytest.mark.asyncio
async def test_update_all_visits_every_collection(tmp_path: Path) -> None:
    """UpdateAllConfirm > Yes must iterate through EVERY queued
    collection, not just the first. Catches the regression where
    drive_indexer's queue dequeue silently dropped the next run.

    Strategy: monkeypatch ``app.start_indexer`` to record every
    invocation. After Enter, drain the loop and assert the recorded
    list matches the queue.
    """
    from fnd.tui.settings_screen import UpdateAllConfirm

    names = ["alpha", "beta", "gamma"]
    cfg, index_dir = _make_cfg(tmp_path, names)
    app = FNDApp(index_dir=index_dir, config=cfg)

    invocations: list[str] = []
    original_start = app.start_indexer

    def _recorder(*, collection: str, **kw: object) -> bool:
        invocations.append(collection)
        return original_start(collection=collection, **kw)  # type: ignore[arg-type]

    app.start_indexer = _recorder  # type: ignore[method-assign]

    async with app.run_test(size=(120, 40)) as pilot:
        await safe_pause(pilot)
        app.push_screen(UpdateAllConfirm(collection_names=names))
        await safe_pause(pilot)
        await pilot.press("enter")
        await wait_until(
            pilot,
            lambda: len(invocations) >= len(names),
            timeout=15.0,
            message=f"chain never invoked {len(names)} collections; saw {invocations}",
        )
        # One more tick to let the final task finish.
        await safe_pause(pilot)

    assert invocations == names, (
        f"Expected start_indexer to fire once per collection in queue order; got {invocations}."
    )


@pytest.mark.asyncio
async def test_update_all_sets_chain_total_for_modal_title(tmp_path: Path) -> None:
    """The IndexerScreen reads ``_indexer_chain_total`` to render the
    ``(X of Y)`` title. UpdateAllConfirm must set it BEFORE the first
    indexer run. After the chain completes, drive_indexer resets it
    to 1, so we capture the value at the moment start_indexer fires."""
    from fnd.tui.settings_screen import UpdateAllConfirm

    names = ["alpha", "beta"]
    cfg, index_dir = _make_cfg(tmp_path, names)
    app = FNDApp(index_dir=index_dir, config=cfg)

    captured_totals: list[int] = []
    original_start = app.start_indexer

    def _capture(*, collection: str, **kw: object) -> bool:
        captured_totals.append(getattr(app, "_indexer_chain_total", 0))
        return original_start(collection=collection, **kw)  # type: ignore[arg-type]

    app.start_indexer = _capture  # type: ignore[method-assign]

    async with app.run_test(size=(120, 40)) as pilot:
        await safe_pause(pilot)
        app.push_screen(UpdateAllConfirm(collection_names=names))
        await safe_pause(pilot)
        await pilot.press("enter")
        await wait_until(
            pilot,
            lambda: len(captured_totals) >= 2,
            timeout=15.0,
            message=f"chain never reached the second collection; captured {captured_totals}",
        )

    # First start_indexer call sees the configured total. The chain's
    # subsequent call also sees it (drive_indexer only resets on the
    # final run's completion).
    assert captured_totals, "start_indexer was never called"
    assert captured_totals[0] == 2, (
        f"Expected _indexer_chain_total=2 at the first start_indexer call; got {captured_totals}."
    )


# ── Index-menu action → run-mode flag routing (non-flaky: no async drive) ──


def _capture_confirm(app: FNDApp) -> list[object]:
    """Replace push_screen with a recorder so a menu handler's pushed
    UpdateAllConfirm can be inspected without a running event loop."""
    captured: list[object] = []
    app.push_screen = lambda screen, *a, **k: captured.append(screen)  # type: ignore[method-assign,assignment]
    return captured


def test_update_all_confirm_stores_run_mode_flags(tmp_path: Path) -> None:
    from fnd.tui.settings_screen import UpdateAllConfirm

    c = UpdateAllConfirm(collection_names=["x"], skip_unchanged=False, force_fresh=True)
    assert c._skip_unchanged is False
    assert c._force_fresh is True


def test_process_new_files_is_incremental_index_only(tmp_path: Path) -> None:
    """'Process new files (index only)' → incremental, no texturising,
    no forced re-extraction."""
    from fnd.tui.menu import _run_update_all_index_only
    from fnd.tui.settings_screen import UpdateAllConfirm

    cfg, index_dir = _make_cfg(tmp_path, ["alpha"])
    app = FNDApp(index_dir=index_dir, config=cfg)
    captured = _capture_confirm(app)
    _run_update_all_index_only(app)
    assert captured, "no confirm pushed"
    c = captured[0]
    assert isinstance(c, UpdateAllConfirm)
    assert c._texturise_override is False
    assert c._skip_unchanged is True
    assert c._force_fresh is False


def test_rebuild_all_collections_forces_fresh_and_rebuilds(tmp_path: Path) -> None:
    """'Rebuild all collections' → drop chunks + re-extract everything
    fresh: rebuild on, force_fresh on, incremental skip OFF, texturising
    forced on."""
    from fnd.tui.menu import _run_rebuild_all_collections
    from fnd.tui.settings_screen import UpdateAllConfirm

    cfg, index_dir = _make_cfg(tmp_path, ["alpha"])
    app = FNDApp(index_dir=index_dir, config=cfg)
    captured = _capture_confirm(app)
    _run_rebuild_all_collections(app)
    assert captured, "no confirm pushed"
    c = captured[0]
    assert isinstance(c, UpdateAllConfirm)
    assert c._texturise_override is True
    assert c._skip_unchanged is False
    assert c._force_fresh is True
    assert c._rebuild is True

"""Flat-PDF scan must never run on the UI thread.

The scan (``settings_screen._flat_pdfs_with_reasons``) costs seconds on a
real corpus. Routed through ``flat_pdf_scan`` it runs in a daemon thread;
the event loop only ever reads the cached result. These tests pin that
contract so the portal-open freeze cannot regress.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from fnd.tui import flat_pdf_scan


class _StubApp:
    """call_from_thread just invokes inline (no loop in unit tests)."""

    def call_from_thread(self, fn: Callable[..., object], *args: object) -> None:
        fn(*args)


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_cached_count_is_none_before_first_scan() -> None:
    flat_pdf_scan.invalidate_all()
    assert flat_pdf_scan.cached_count() is None
    assert flat_pdf_scan.cached_rows() is None


def test_schedule_runs_compute_off_the_calling_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """The expensive scan executes on a worker thread, not the caller's."""
    flat_pdf_scan.invalidate_all()
    main = threading.current_thread()
    seen: dict[str, object] = {}
    done = threading.Event()

    def _fake_scan(*, collection: str | None = None) -> list[flat_pdf_scan.Row]:
        seen["thread"] = threading.current_thread()
        return [("c", "/x.pdf", "flat", None)]

    monkeypatch.setattr("fnd.tui.settings_screen._flat_pdfs_with_reasons", _fake_scan)

    ready: list[list[flat_pdf_scan.Row]] = []

    def _on_ready(rows: list[flat_pdf_scan.Row]) -> None:
        ready.append(rows)
        done.set()

    flat_pdf_scan.schedule_refresh(_StubApp(), None, on_ready=_on_ready)

    assert done.wait(2.0), "worker should complete"
    assert seen["thread"] is not main, "scan must not run on the calling thread"
    assert flat_pdf_scan.cached_count() == 1
    assert ready
    assert ready[0][0][1] == "/x.pdf"


def test_fresh_cache_notifies_inline_without_recompute(monkeypatch: pytest.MonkeyPatch) -> None:
    flat_pdf_scan.invalidate_all()
    calls = {"n": 0}

    def _fake_scan(*, collection: str | None = None) -> list[flat_pdf_scan.Row]:
        calls["n"] += 1
        return [("c", "/x.pdf", "flat", None)]

    monkeypatch.setattr("fnd.tui.settings_screen._flat_pdfs_with_reasons", _fake_scan)

    first_done = threading.Event()
    flat_pdf_scan.schedule_refresh(_StubApp(), None, on_ready=lambda _rows: first_done.set())
    assert first_done.wait(2.0)
    assert _wait_until(lambda: flat_pdf_scan.cached_count() == 1)

    # Second schedule within TTL must reuse the cache (no second scan)
    # and still deliver the rows synchronously to the caller.
    got: list[list[flat_pdf_scan.Row]] = []
    flat_pdf_scan.schedule_refresh(_StubApp(), None, on_ready=got.append)
    assert got
    assert got[0][0][1] == "/x.pdf"
    assert calls["n"] == 1, "fresh cache must not trigger a recompute"


def test_invalidate_forces_recompute(monkeypatch: pytest.MonkeyPatch) -> None:
    flat_pdf_scan.invalidate_all()
    calls = {"n": 0}

    def _fake_scan(*, collection: str | None = None) -> list[flat_pdf_scan.Row]:
        calls["n"] += 1
        return [("c", f"/x{calls['n']}.pdf", "flat", None)]

    monkeypatch.setattr("fnd.tui.settings_screen._flat_pdfs_with_reasons", _fake_scan)

    d1 = threading.Event()
    flat_pdf_scan.schedule_refresh(_StubApp(), None, on_ready=lambda _r: d1.set())
    assert d1.wait(2.0)
    assert _wait_until(lambda: calls["n"] == 1)

    flat_pdf_scan.invalidate()
    d2 = threading.Event()
    flat_pdf_scan.schedule_refresh(_StubApp(), None, on_ready=lambda _r: d2.set())
    assert d2.wait(2.0)
    assert _wait_until(lambda: calls["n"] == 2)


def test_concurrent_schedules_share_one_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two schedules for the same scope while a scan is in flight run the
    scan once and notify both callers."""
    flat_pdf_scan.invalidate_all()
    calls = {"n": 0}
    gate = threading.Event()

    def _fake_scan(*, collection: str | None = None) -> list[flat_pdf_scan.Row]:
        calls["n"] += 1
        gate.wait(2.0)  # hold the worker open so the 2nd schedule joins
        return [("c", "/x.pdf", "flat", None)]

    monkeypatch.setattr("fnd.tui.settings_screen._flat_pdfs_with_reasons", _fake_scan)

    n1 = threading.Event()
    n2 = threading.Event()
    flat_pdf_scan.schedule_refresh(_StubApp(), None, on_ready=lambda _r: n1.set())
    # Second schedule arrives mid-flight; must not start a 2nd scan.
    flat_pdf_scan.schedule_refresh(_StubApp(), None, on_ready=lambda _r: n2.set())
    gate.set()

    assert n1.wait(2.0)
    assert n2.wait(2.0)
    assert calls["n"] == 1


# ── Regression: the portal must never run the scan on the UI thread ──


@pytest.mark.asyncio
async def test_indexer_modal_never_scans_on_ui_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mounting IndexerScreen and letting it tick must not call the
    flat-PDF scan on the event-loop thread — that synchronous call was
    the multi-second portal-open freeze. Any call must land on a worker
    thread."""
    from fnd.config import load
    from fnd.index import build_index
    from fnd.tui import FNDApp
    from fnd.tui.indexer_modal import IndexerScreen

    flat_pdf_scan.invalidate_all()

    threads_seen: list[threading.Thread] = []
    loop_thread = threading.current_thread()

    def _spy_scan(*, collection: str | None = None) -> list[flat_pdf_scan.Row]:
        threads_seen.append(threading.current_thread())
        time.sleep(0.05)  # emulate a slow scan
        return [("default", "/flat.pdf", "still flat", None)]

    monkeypatch.setattr("fnd.tui.settings_screen._flat_pdfs_with_reasons", _spy_scan)

    index_dir = tmp_path / "index"
    fixtures = Path(__file__).parent / "fixtures"
    build_index(roots=[fixtures], index_dir=index_dir, collection="default")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("")
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    cfg = load(cfg_path)

    app = FNDApp(index_dir=index_dir, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.push_screen(IndexerScreen("default"))
        # Pump the event loop across several 1Hz ticks; if the scan ran
        # inline on any of them the loop thread would appear below.
        for _ in range(20):
            await pilot.pause(0.05)

    assert threads_seen, "the background scan should have run at least once"
    assert loop_thread not in threads_seen, (
        "flat-PDF scan ran on the event-loop thread — portal would freeze"
    )


@pytest.mark.asyncio
async def test_still_flat_drillin_never_scans_on_ui_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Flat-PDFs drill-in must paint a placeholder and scan off-loop,
    then render the rows the worker produced."""
    from fnd.config import load
    from fnd.index import build_index
    from fnd.tui import FNDApp
    from fnd.tui.settings_screen import StillFlatDrillIn

    flat_pdf_scan.invalidate_all()
    threads_seen: list[threading.Thread] = []
    loop_thread = threading.current_thread()

    def _spy_scan(*, collection: str | None = None) -> list[flat_pdf_scan.Row]:
        threads_seen.append(threading.current_thread())
        time.sleep(0.05)
        return [("default", "/flat.pdf", "still flat", None)]

    monkeypatch.setattr("fnd.tui.settings_screen._flat_pdfs_with_reasons", _spy_scan)

    index_dir = tmp_path / "index"
    fixtures = Path(__file__).parent / "fixtures"
    build_index(roots=[fixtures], index_dir=index_dir, collection="default")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("")
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    cfg = load(cfg_path)

    app = FNDApp(index_dir=index_dir, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.push_screen(StillFlatDrillIn(collection=None))
        rendered = False
        for _ in range(30):
            await pilot.pause(0.05)
            body = app.screen.query_one("#still_flat_body")
            if any("flat.pdf" in str(c.render()) for c in body.children):
                rendered = True
                break

    assert threads_seen
    assert loop_thread not in threads_seen
    assert rendered, "rows from the background scan should eventually render"

"""A malformed query surfaces a calm inline notice below the query bar and
never crashes the app; a valid query clears it."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Static

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp
from tests._pilot_wait import run_search


def _write(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    p = tmp_path / "config.toml"
    p.write_text(
        textwrap.dedent("""
            [[collections.notes.sources]]
            path = "/tmp/notes"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: p)
    return load(p)


@pytest.fixture
def md_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    a = tmp_path / "notes"
    _write(a / "Notes.md", "# Patterns\n\nThe templates pattern is described here.\n")
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_malformed_proximity_notice_then_clears(cfg: Config, md_index: Path) -> None:
    app = FNDApp(index_dir=md_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        # A malformed query never reaches the worker: prepare rejects it on
        # the loop so the notice is up on the same frame.
        await run_search(pilot, app, "{60}")
        notice = app.query_one("#query_notice", Static)
        assert notice.display is True
        assert "proximity" in str(notice.render()).lower()

        await run_search(pilot, app, "templates")
        assert app.query_one("#query_notice", Static).display is False


@pytest.mark.asyncio
async def test_malformed_query_resets_explain_trace(cfg: Config, md_index: Path) -> None:
    """Issue #61: a malformed query rejected at plan-validation must drop the
    last good trace so ``:explain`` can't show a stale plan."""
    app = FNDApp(index_dir=md_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "templates")
        assert app._search.latest_trace is not None
        app._search.run("{60}")  # malformed → rejected at QueryPlan
        assert app._search.latest_trace is None


@pytest.mark.asyncio
async def test_layered_error_resets_explain_trace(
    cfg: Config, md_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #61: an error raised from the layered search path also drops the
    last good trace."""
    from fnd.query_errors import QuerySyntaxError

    app = FNDApp(index_dir=md_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "templates")
        assert app._search.latest_trace is not None

        def _boom(_request: object) -> tuple[list[object], object]:
            raise QuerySyntaxError("boom")

        # The search itself now runs on a worker thread, so this is where a
        # layered-path error surfaces; _commit_failure marshals it back.
        monkeypatch.setattr(app._search, "_execute", _boom)
        await run_search(pilot, app, "anything")
        assert app._search.latest_trace is None

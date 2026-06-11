"""UXP-4 §2 (TUI surface) — :explain overlay."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp


def _write_md(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.notes.sources]]
            path = "/tmp/notes"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.fixture
def small_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    a = tmp_path / "notes"
    _write_md(
        a / "doc.md",
        "# Doc\n\nmitochondrion is the powerhouse of the cell. "
        "mitochondrion mitochondrion mitochondrion.\n",
    )
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_explain_overlay_captures_trace_and_toggles(cfg: Config, small_index: Path) -> None:
    """After a search, the trace is captured on the app and :explain
    pops an overlay. A second call toggles the overlay closed."""
    app = FNDApp(index_dir=small_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        app._search.run("mitochondrion")
        await pilot.pause()
        # Trace captured on the app — regime depends on corpus IDF, but
        # the trace itself must always be populated when groups exist.
        assert app._search.latest_trace is not None
        assert app._search.latest_trace.query == "mitochondrion"
        assert app._search.latest_trace.regime in {
            "strong-signal",
            "fusion",
            "cascade",
            "cascade(+fuzzy)",
            "cascade(+syn)",
            "cascade(+fuzzy+syn)",
        }
        # Overlay opens
        app.action_show_explain_overlay()
        await pilot.pause()
        overlay = app.query("#explain_overlay")
        assert len(overlay) == 1
        # Toggle: a second call dismisses the overlay
        app.action_show_explain_overlay()
        await pilot.pause()
        assert len(app.query("#explain_overlay")) == 0


@pytest.mark.asyncio
async def test_explain_overlay_no_search_warns(cfg: Config, small_index: Path) -> None:
    """Calling :explain before any search emits a notify, no overlay."""
    app = FNDApp(index_dir=small_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        # No query run; trace is None
        assert app._search.latest_trace is None
        app.action_show_explain_overlay()
        await pilot.pause()
        # No overlay mounted
        assert len(app.query("#explain_overlay")) == 0


@pytest.mark.asyncio
async def test_dismiss_overlay_closes_explain(cfg: Config, small_index: Path) -> None:
    """action_dismiss_overlay also clears #explain_overlay."""
    app = FNDApp(index_dir=small_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        app._search.run("mitochondrion")
        await pilot.pause()
        app.action_show_explain_overlay()
        await pilot.pause()
        assert len(app.query("#explain_overlay")) == 1
        app.action_dismiss_overlay()
        await pilot.pause()
        assert len(app.query("#explain_overlay")) == 0

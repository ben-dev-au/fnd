"""The dynamic allocator, wired into the app: a short filters list must not
out-size the results pane (the reported bug), and collapsing/expanding a panel
must re-derive the shares.

Heights resolve under ``run_test`` at an explicit size (the layout pass runs);
these assert the coarse relationship the allocator guarantees, not exact rows.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Tree

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp


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
def idx(tmp_path: Path, tmp_index_dir: Path) -> Path:
    root = tmp_path / "notes"
    root.mkdir(parents=True, exist_ok=True)
    for i in range(30):  # many results so the results pane genuinely wants space
        (root / f"n{i}.md").write_text(
            f"---\ntags: [t{i}]\n---\n\n# N{i}\n\nsaffron\n", encoding="utf-8"
        )
    build_index(roots=[root], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_short_filters_pane_does_not_outsize_results(cfg: Config, idx: Path) -> None:
    app = FNDApp(index_dir=idx, config=cfg, initial_query="saffron")
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        await pilot.pause()
        results = app.query_one("#results_pane", Tree)
        filters_pane = app.query_one("#filters_pane")
        # The bug: a 4-row filters pane ballooned to ~half the column while the
        # 30-result list was squeezed. The allocator must invert that.
        assert results.region.height > filters_pane.region.height
        assert filters_pane.region.height <= 10, (
            f"filters pane {filters_pane.region.height} rows for a handful of "
            "filter rows — it ballooned"
        )


@pytest.mark.asyncio
async def test_reflow_runs_and_fills_the_column(cfg: Config, idx: Path) -> None:
    app = FNDApp(index_dir=idx, config=cfg, initial_query="saffron")
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        await pilot.pause()
        # The reflow ran (cache populated) and the three panels tile the column
        # with no floating gap.
        assert app._sidebar_height_cache, "reflow never applied any height"
        column = app.query_one("#results_column")
        panes = [
            app.query_one("#results_pane"),
            app.query_one("#collections_panel_tree"),
            app.query_one("#filters_pane"),
        ]
        assert sum(p.region.height for p in panes) == column.content_region.height


@pytest.mark.asyncio
async def test_collapsing_results_hands_space_to_the_others(cfg: Config, idx: Path) -> None:
    app = FNDApp(index_dir=idx, config=cfg, initial_query="saffron")
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        await pilot.pause()
        filters_before = app.query_one("#filters_pane").region.height

        # Collapse the results pane to its header; the freed rows go to the
        # expanded panels.
        app.query_one("#results_pane", Tree).add_class("collapsed")
        app._reflow_sidebar()
        await pilot.pause()
        await pilot.pause()

        assert app.query_one("#results_pane", Tree).region.height <= 3
        assert app.query_one("#filters_pane").region.height >= filters_before

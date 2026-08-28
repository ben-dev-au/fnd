"""Clicking the collapsed filters pane must reopen it, like every other panel.

The filters tree is wrapped in ``#filters_pane`` which carries the collapse
class and, at header height, the only clickable rows (the tree has zero height
when collapsed). So the click has to be caught on the container and the reopen
has to act on that frame — not the tree, which is what results/collections use.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Tree

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp
from tests._pilot_wait import wait_until


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
    for i in range(8):
        (root / f"n{i}.md").write_text(
            f"---\ntags: [t{i}]\n---\n\n# N{i}\n\nsaffron\n", encoding="utf-8"
        )
    build_index(roots=[root], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


def _collapse_filters(app: FNDApp) -> None:
    app.query_one("#filters_pane").add_class("collapsed")
    app._scope.collapsed_panels.add("filters_pane")
    app._reflow_sidebar()


@pytest.mark.asyncio
async def test_clicking_collapsed_filters_pane_reopens_it(cfg: Config, idx: Path) -> None:
    app = FNDApp(index_dir=idx, config=cfg, initial_query="saffron")
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        _collapse_filters(app)
        await pilot.pause()
        await pilot.pause()
        assert "collapsed" in app.query_one("#filters_pane").classes

        await pilot.click("#filters_pane")
        await pilot.pause()
        await pilot.pause()

        assert "collapsed" not in app.query_one("#filters_pane").classes, (
            "clicking the collapsed filters pane should reopen it"
        )
        assert "filters_pane" not in app._scope.collapsed_panels


@pytest.mark.asyncio
async def test_clicking_collapsed_results_pane_still_reopens(cfg: Config, idx: Path) -> None:
    """Regression guard for the unwrapped panels — the click path they use must
    keep working."""
    app = FNDApp(index_dir=idx, config=cfg, initial_query="saffron")
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        results = app.query_one("#results_pane", Tree)
        results.add_class("collapsed")
        app._scope.collapsed_panels.add("results_pane")
        app._reflow_sidebar()
        await pilot.pause()
        await pilot.pause()
        assert "collapsed" in results.classes

        await pilot.click("#results_pane")
        await pilot.pause()
        await pilot.pause()

        assert "collapsed" not in app.query_one("#results_pane", Tree).classes


@pytest.mark.asyncio
async def test_clicking_expanded_filters_pane_is_not_swallowed(cfg: Config, idx: Path) -> None:
    """The container click handler must only act while collapsed — an expanded
    filters pane's clicks still reach the tree (it takes focus), so filter rows
    stay togglable by mouse."""
    app = FNDApp(index_dir=idx, config=cfg, initial_query="saffron")
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        assert "collapsed" not in app.query_one("#filters_pane").classes

        tree = app.query_one("#filters_panel_tree", Tree)
        # Click the centre of a widget that has been through layout: clicking a
        # zero-region one sends the event somewhere else entirely, and waiting
        # for focus afterwards can only ever time out.
        await wait_until(
            pilot,
            lambda: tree.region.width > 0 and tree.region.height > 0,
            timeout=30.0,
            message="the filters tree never laid out, so a click cannot target it",
        )
        landed = await pilot.click("#filters_panel_tree")
        assert landed, (
            f"click missed the filters tree (region={tree.region}); focus could never have followed"
        )
        # Focus is posted, not synchronous.
        await wait_until(
            pilot,
            lambda: app.focused is tree,
            timeout=30.0,
            message="clicking the expanded filters pane never moved focus to its tree",
        )
        assert "collapsed" not in app.query_one("#filters_pane").classes
        assert app.focused is app.query_one("#filters_panel_tree", Tree)

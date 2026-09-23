"""The Results pane marks its title when collapsed. Its two siblings did not.

`←` on an already-collapsed top-level row shrinks a sidebar panel to its two
border rows, and the state persists to `scope.toml`. For Collections and
Filters the title was byte-identical to an open panel's, so the pane read as
empty rather than closed, while the title went on asserting a scope the user
could no longer see or reach.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Tree

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp
from tests._pilot_wait import wait_until


@pytest.fixture
def indexed(tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    root = tmp_path / "notes"
    root.mkdir()
    for i in range(4):
        (root / f"n{i}.md").write_text(f"# N{i}\n\nsaffron here.\n", encoding="utf-8")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.notes.sources]]
            path = "{root.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    build_index(roots=[root], index_dir=tmp_index_dir, collection="notes")
    return load(cfg_path)


async def _collapse(
    pilot: Pilot[None], app: FNDApp, tree_id: str, frame_id: str
) -> tuple[str, str]:
    """Collapse that panel to its header, returning its title before and after."""
    tree = app.query_one(f"#{tree_id}", Tree)
    # The filters tree is wrapped in `#filters_pane`, which carries the border
    # and the title; the collections tree is its own frame.
    titled = app.query_one(f"#{frame_id}")
    await wait_until(
        pilot,
        lambda: bool(tree.root.children) and bool(str(titled.border_title or "")),
        timeout=30.0,
        message=f"#{tree_id} never populated",
    )
    tree.focus()
    # The action reads `_focus_context()`, so a focus that has not landed
    # sends every press to whichever tree still holds it.
    await wait_until(
        pilot,
        lambda: app.focused is tree,
        timeout=10.0,
        message=f"#{tree_id} never took focus",
    )
    before = str(titled.border_title or "")
    # Once per level: each press folds the focused branch, and the press that
    # finds nothing left to fold closes the panel.
    for _ in range(4):
        if "collapsed" in titled.classes:
            break
        app.action_tree_smart_collapse()
        await pilot.pause()
    await wait_until(
        pilot,
        lambda: "collapsed" in titled.classes,
        timeout=10.0,
        message=f"#{frame_id} never collapsed",
    )
    after = str(titled.border_title or "")
    return before, after


@pytest.mark.asyncio
async def test_a_collapsed_collections_panel_marks_its_title(
    indexed: Config, tmp_index_dir: Path
) -> None:
    app = FNDApp(
        index_dir=tmp_index_dir, config=indexed, collection="notes", initial_query="saffron"
    )
    async with app.run_test(size=(100, 30)) as pilot:
        before, after = await _collapse(
            pilot, app, "collections_panel_tree", "collections_panel_tree"
        )

    assert after != before, "a collapsed panel looked exactly like an open one"
    assert "▶" in after, after
    assert "active" in after, "the counts are still true and still wanted"


@pytest.mark.asyncio
async def test_a_collapsed_filters_pane_marks_its_title(
    indexed: Config, tmp_index_dir: Path
) -> None:
    app = FNDApp(
        index_dir=tmp_index_dir, config=indexed, collection="notes", initial_query="saffron"
    )
    async with app.run_test(size=(100, 30)) as pilot:
        before, after = await _collapse(pilot, app, "filters_panel_tree", "filters_pane")

    assert after != before, "a collapsed pane looked exactly like an open one"
    assert "▶" in after, after
    assert "Filters" in after, after


@pytest.mark.asyncio
async def test_reopening_takes_the_mark_off(indexed: Config, tmp_index_dir: Path) -> None:
    """The control: a mark that outlives its state is worse than none."""
    app = FNDApp(
        index_dir=tmp_index_dir, config=indexed, collection="notes", initial_query="saffron"
    )
    async with app.run_test(size=(100, 30)) as pilot:
        _, collapsed = await _collapse(
            pilot, app, "collections_panel_tree", "collections_panel_tree"
        )
        assert "▶" in collapsed, collapsed

        tree = app.query_one("#collections_panel_tree", Tree)
        app.action_tree_smart_expand()
        await wait_until(
            pilot,
            lambda: "collapsed" not in tree.classes,
            timeout=10.0,
            message="the panel never reopened",
        )
        after = str(tree.border_title or "")

    assert "▶" not in after, after

"""Two `←` presses collapse the Results pane, and it looked identical to an
open one.

The collapse is deliberate (it is the lazygit section gesture), but the title
went on reading `32 files / 269 sections` with no chevron and no
marker, and the state persists to `scope.toml`, so six fresh sessions in six
came up looking as though the results had simply gone.
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
def indexed(tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    root = tmp_path / "notes"
    root.mkdir()
    for i in range(4):
        (root / f"n{i}.md").write_text(
            f"# N{i}\n\nsaffron here.\n\n## More\n\nsaffron again.\n", encoding="utf-8"
        )
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


@pytest.mark.asyncio
async def test_collapsing_the_pane_marks_its_title(indexed: Config, tmp_index_dir: Path) -> None:
    app = FNDApp(
        index_dir=tmp_index_dir, config=indexed, collection="notes", initial_query="saffron"
    )
    async with app.run_test(size=(100, 30)) as pilot:
        for _ in range(25):
            await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        tree.focus()
        await pilot.pause()
        open_title = str(tree.border_title)

        app.action_tree_smart_collapse()
        app.action_tree_smart_collapse()
        for _ in range(8):
            await pilot.pause()
        collapsed_title = str(tree.border_title)
        collapsed = "collapsed" in tree.classes

    assert collapsed, "the pane did not collapse; the rest proves nothing"
    assert collapsed_title != open_title, "a collapsed pane looked exactly like an open one"
    assert "▶" in collapsed_title, collapsed_title
    assert "files" in collapsed_title, "the counts are still true and still wanted"


@pytest.mark.asyncio
async def test_expanding_it_takes_the_mark_off(indexed: Config, tmp_index_dir: Path) -> None:
    """The control: the marker must not outlive the state it describes."""
    app = FNDApp(
        index_dir=tmp_index_dir, config=indexed, collection="notes", initial_query="saffron"
    )
    async with app.run_test(size=(100, 30)) as pilot:
        for _ in range(25):
            await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        tree.focus()
        await pilot.pause()
        app.action_tree_smart_collapse()
        app.action_tree_smart_collapse()
        for _ in range(8):
            await pilot.pause()
        assert "▶" in str(tree.border_title)

        app.action_tree_smart_expand()
        for _ in range(8):
            await pilot.pause()
        after = str(tree.border_title)

    assert "▶" not in after, after
    assert "collapsed" not in tree.classes

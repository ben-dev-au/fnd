"""`End` reached the last result and `Home` did nothing at all.

The scroll view binds both, but scrolling a viewport the cursor does not follow
leaves the pane looking frozen. Measured: End took the cursor from row 1 to
row 30 and scrolled 23 lines, Home moved neither.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.widgets.results_tree import ResultsTree


@pytest.fixture
def many(tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    root = tmp_path / "notes"
    root.mkdir()
    for i in range(30):
        (root / f"n{i:02d}.md").write_text(f"# N{i}\n\nsaffron.\n", encoding="utf-8")
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
async def test_home_comes_back_to_the_top(many: Config, tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir, config=many, collection="notes", initial_query="saffron")
    async with app.run_test(size=(100, 20)) as pilot:
        for _ in range(30):
            await pilot.pause()
        tree = app.query_one("#results_pane", ResultsTree)
        tree.focus()
        await pilot.pause()

        await pilot.press("end")
        for _ in range(4):
            await pilot.pause()
        at_end = (tree.cursor_line, tree.scroll_offset.y)

        await pilot.press("home")
        for _ in range(4):
            await pilot.pause()
        at_home = (tree.cursor_line, tree.scroll_offset.y)

    assert at_end[0] > 1, "End must still reach the bottom"
    assert at_home[0] < at_end[0], f"Home moved nothing: {at_end} -> {at_home}"
    assert at_home[1] == 0, f"and the view must come back with it: {at_home}"


def test_every_tree_answers_it() -> None:
    """Class-wide: the three trees a user can focus all bind it."""
    from textual.binding import Binding

    from fnd.tui.widgets.scope_tree import ScopeTree
    from fnd.tui.widgets.toggle_tree import ToggleTree

    for cls in (ResultsTree, ScopeTree, ToggleTree):
        keys = {
            b.key if isinstance(b, Binding) else b[0]
            for b in cls.__dict__.get("BINDINGS", ())  # type: ignore[union-attr]
        }
        assert "home" in keys, cls.__name__
        assert hasattr(cls, "action_cursor_first"), cls.__name__

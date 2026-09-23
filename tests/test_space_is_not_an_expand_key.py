"""Space is not an expand key: arrows own expansion, and space reaches no tree.

Textual's stock `toggle_node` binds space to expand/collapse, while on a
settings filter row space toggles the selection: one key doing two things in
two panes of the same screen.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Tree

from fnd.config import load
from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.widgets.arrow_expansion import ArrowsExpand
from fnd.tui.widgets.results_tree import ResultsTree
from fnd.tui.widgets.scope_tree import ScopeTree
from fnd.tui.widgets.toggle_tree import ToggleGroup, ToggleItem, ToggleTree


def _fnd_tree_classes() -> list[type[Tree[Any]]]:
    """Every ``Tree`` subclass fnd defines; importing the app is what defines them."""
    import fnd.tui.app  # noqa: F401  # pyright: ignore[reportUnusedImport]

    found: list[type[Tree[Any]]] = []
    stack: list[type[Tree[Any]]] = list(Tree.__subclasses__())
    while stack:
        cls = stack.pop()
        stack.extend(cls.__subclasses__())
        if cls.__module__.startswith("fnd."):
            found.append(cls)
    return found


def test_no_fnd_tree_answers_space() -> None:
    """The class-wide guard: a tree added later inherits the answer or fails."""
    classes = _fnd_tree_classes()
    assert len(classes) >= 3, classes
    for cls in classes:
        assert issubclass(cls, ArrowsExpand), f"{cls.__name__} still answers space"
        keys = {
            b.key if isinstance(b, Binding) else b[0]
            for b in cls.__dict__.get("BINDINGS", ())  # type: ignore[union-attr]
        }
        assert "space" not in keys, f"{cls.__name__} binds space"


class _SidebarHarness(App[None]):
    """A collapsed parent in each sidebar tree, cursor parked on it."""

    def compose(self) -> ComposeResult:
        yield ScopeTree("Collections", id="scope")
        yield ResultsTree("Filters", id="filters")

    def on_mount(self) -> None:
        for widget_id in ("#scope", "#filters"):
            tree: Tree[Any] = self.query_one(widget_id, Tree)
            tree.show_root = False
            parent = tree.root.add("Parent", expand=False)
            parent.add_leaf("Child")
            tree.cursor_line = 0
        self.query_one("#scope", ScopeTree).focus()


@pytest.mark.asyncio
@pytest.mark.parametrize("widget_id", ["#scope", "#filters"])
async def test_space_does_not_expand_a_sidebar_row(widget_id: str) -> None:
    """The widget half. ←/→ are app actions, so the control is the test below."""
    app = _SidebarHarness()
    async with app.run_test() as pilot:
        await pilot.pause()
        tree: Tree[Any] = app.query_one(widget_id, Tree)
        tree.focus()
        await pilot.pause()
        node = tree.root.children[0]

        await pilot.press("space")
        await pilot.pause()
        assert not node.is_expanded, "space expanded the row"


@pytest.fixture
def indexed_app(tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch) -> FNDApp:
    docs = tmp_path / "docs"
    docs.mkdir()
    for i in range(4):
        (docs / f"file{i}.md").write_text(f"# Title {i}\n\nglimmer number {i}.\n", encoding="utf-8")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.notes.sources]]
            path = "{docs.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    build_index(roots=[docs], index_dir=tmp_index_dir, collection="notes")
    return FNDApp(
        config=load(cfg_path),
        index_dir=tmp_index_dir,
        collection="notes",
        initial_query="glimmer",
    )


@pytest.mark.asyncio
async def test_the_arrow_still_expands_where_space_did(indexed_app: FNDApp) -> None:
    """The control: removing a key must not strand the pane without one."""
    app = indexed_app
    async with app.run_test(size=(120, 40)) as pilot:
        for _ in range(8):
            await pilot.pause()
        tree = app.query_one("#results_pane", ResultsTree)
        tree.focus()
        tree.root.children[0].collapse()
        await pilot.pause()
        node = tree.root.children[-1]
        tree.move_cursor(node)
        await pilot.pause()
        assert not node.is_expanded

        await pilot.press("space")
        await pilot.pause()
        assert not node.is_expanded, "space expanded the row"

        await pilot.press("right")
        for _ in range(3):
            await pilot.pause()
        assert node.is_expanded, "the arrows must still expand it"


class _ToggleHarness(App[None]):
    def compose(self) -> ComposeResult:
        yield ToggleTree(id="tt")

    def on_mount(self) -> None:
        tree = self.query_one("#tt", ToggleTree)
        tree.set_model(
            [ToggleGroup("code", "Code", (ToggleItem("py", "Python"),))],
            set(),
            expanded={"code"},
        )
        tree.focus()


@pytest.mark.asyncio
async def test_space_does_not_toggle_a_filter_row() -> None:
    app = _ToggleHarness()
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#tt", ToggleTree)
        tree.cursor_line = 1  # Python
        await pilot.pause()

        await pilot.press("space")
        await pilot.pause()
        assert tree.selected == set(), "space toggled the row"

        await pilot.press("enter")
        await pilot.pause()
        assert tree.selected == {"py"}, "Enter must still toggle it"

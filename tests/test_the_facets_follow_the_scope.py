"""Ticking a collection did not recompute the facets it brings into scope.

The tag and file-type rows are index-derived and scoped to the active
collections. The filter toggles repaint the panel; the collection toggles did
not, so tags living only in the collection just ticked stayed unfilterable
until a search or a restart forced a rebuild.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp


@pytest.fixture
def two_collections(tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    papers = tmp_path / "papers"
    notes = tmp_path / "notes"
    papers.mkdir()
    notes.mkdir()
    (papers / "a.md").write_text("---\ntags: [recipe]\n---\n\nsaffron\n", encoding="utf-8")
    (notes / "b.md").write_text("---\ntags: [journal]\n---\n\nsaffron\n", encoding="utf-8")
    build_index(roots=[papers], index_dir=tmp_index_dir, collection="papers")
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.papers.sources]]
            path = "{papers.as_posix()}"

            [[collections.notes.sources]]
            path = "{notes.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


def _branch(tree: Tree[Any], label: str) -> TreeNode[Any]:
    for node in tree.root.children:
        if label in str(node.label):
            return node
    raise AssertionError(f"{label!r} not in {[str(n.label) for n in tree.root.children]}")


async def _tags(app: FNDApp, pilot: Any) -> str:
    tree = app.query_one("#filters_panel_tree", Tree)
    tags = _branch(tree, "Tags")
    tags.expand()
    await pilot.pause()
    out: list[str] = []

    def walk(node: TreeNode[Any]) -> None:
        out.append(str(node.label))
        for c in node.children:
            c.expand()
            walk(c)

    walk(tags)
    await pilot.pause()
    return " ".join(out)


@pytest.mark.asyncio
async def test_ticking_a_collection_brings_its_tags_into_the_panel(
    two_collections: Config, tmp_index_dir: Path
) -> None:
    app = FNDApp(index_dir=tmp_index_dir, config=two_collections)
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        from fnd.tui.scope_panel import FULL

        app._scope.selection = {"papers": FULL}
        app._scope.refresh_filters_panel()
        await pilot.pause()
        assert "journal" not in await _tags(app, pilot), "precondition: notes is out of scope"

        # Toggle it the way a user does: the cursor on the collection row in
        # the sidebar, then Enter.
        ctree = app.query_one("#collections_panel_tree", Tree)
        app._scope.refresh_collections_panel()
        await pilot.pause()
        node = next(n for n in ctree.root.children if "notes" in str(n.label))
        ctree.cursor_line = node.line
        ctree.focus()
        await pilot.pause()
        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause()
        assert "notes" in app._scope.collections, "the toggle landed"

        assert "journal" in await _tags(app, pilot), "the tag it just brought into scope"

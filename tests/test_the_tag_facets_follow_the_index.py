"""After an index run the tag facets describe the index, not the launch snapshot.

`on_reindex_complete` swapped the searcher and invalidated the kinds cache and
left the panel itself unrepainted, so the sidebar went on offering tags with no
files and hiding tags that had them until a search or a toggle forced a rebuild.
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
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.papers.sources]]
            path = "/tmp/papers"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "papers"
    root.mkdir(parents=True, exist_ok=True)
    (root / "keep.md").write_text(
        "---\ntags: [recipe]\n---\n\n# Keep\n\nsaffron and stock\n", encoding="utf-8"
    )
    (root / "drop.md").write_text(
        "---\ntags: [draft]\n---\n\n# Drop\n\nsaffron rub\n", encoding="utf-8"
    )
    return root


def _reindex(root: Path, index_dir: Path) -> None:
    build_index(roots=[root], index_dir=index_dir, collection="papers")


def _branch(tree: Tree[Any], label: str) -> TreeNode[Any]:
    for node in tree.root.children:
        if label in str(node.label):
            return node
    raise AssertionError(f"{label!r} not in {[str(n.label) for n in tree.root.children]}")


def _descend(node: TreeNode[Any], label: str) -> TreeNode[Any]:
    for child in node.children:
        if label in str(child.label):
            return child
    raise AssertionError(f"{label!r} not under {node.label!r}")


async def _tag_labels(app: FNDApp, pilot: Any) -> str:
    tree = app.query_one("#filters_panel_tree", Tree)
    tags = _branch(tree, "Tags")
    tags.expand()
    await pilot.pause()
    fm = _descend(tags, "Frontmatter")
    fm.expand()
    await pilot.pause()
    return " ".join(str(c.label) for c in fm.children)


@pytest.mark.asyncio
async def test_a_tag_whose_files_all_left_stops_being_offered(
    cfg: Config, corpus: Path, tmp_index_dir: Path
) -> None:
    _reindex(corpus, tmp_index_dir)
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "draft" in await _tag_labels(app, pilot), "precondition: the tag is there"

        (corpus / "drop.md").unlink()
        _reindex(corpus, tmp_index_dir)
        app._indexer.on_reindex_complete()
        await pilot.pause()

        labels = await _tag_labels(app, pilot)
        assert "draft" not in labels, f"the index holds no draft file: {labels!r}"
        assert "recipe" in labels, "and the surviving tag is still offered"


@pytest.mark.asyncio
async def test_a_tag_admitted_by_the_run_becomes_offered(
    cfg: Config, corpus: Path, tmp_index_dir: Path
) -> None:
    """The other direction: re-admitting a tag from inside the app must show it."""
    (corpus / "drop.md").unlink()
    _reindex(corpus, tmp_index_dir)
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "draft" not in await _tag_labels(app, pilot), "precondition: it is absent"

        (corpus / "drop.md").write_text(
            "---\ntags: [draft]\n---\n\n# Drop\n\nsaffron rub\n", encoding="utf-8"
        )
        _reindex(corpus, tmp_index_dir)
        app._indexer.on_reindex_complete()
        await pilot.pause()

        assert "draft" in await _tag_labels(app, pilot)

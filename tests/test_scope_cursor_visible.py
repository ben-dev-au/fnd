"""The sidebar's highlighted row must survive a re-search."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.events import Resize
from textual.widgets import Tree

from fnd.config import Config, load
from fnd.tui import FNDApp


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    from fnd.index import build_index

    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.fixture
def tall_config(fixtures_dir: Path, tmp_path: Path) -> Config:
    """Enough collections and sources that the panel must scroll."""
    lines: list[str] = []
    for i in range(8):
        for j in range(2):
            root = tmp_path / f"c{i}s{j}"
            root.mkdir(parents=True, exist_ok=True)
            lines.append(f'[[collections.coll{i}.sources]]\npath = "{root.as_posix()}"')
    lines.append(f'[[collections.zdefault.sources]]\npath = "{fixtures_dir.as_posix()}"')
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return load(cfg_path)


def _visible(tree: Tree) -> bool:  # type: ignore[type-arg]
    top, height, line = tree.scroll_offset.y, tree.size.height, tree.cursor_line
    return bool(height) and top <= line < top + height


async def _deep_tree(app: FNDApp, pilot) -> Tree:  # type: ignore[no-untyped-def, type-arg]
    tree = app.query_one("#collections_panel_tree", Tree)
    for node in tree.root.children:
        node.expand()
    await pilot.pause()
    tree.focus()
    rows = sum(1 + len(n.children) for n in tree.root.children)
    tree.cursor_line = max(rows - 2, 0)
    for _ in range(6):
        await pilot.pause()
    return tree


@pytest.mark.asyncio
async def test_a_viewport_change_does_not_strand_the_cursor(
    built_index: Path, tall_config: Config
) -> None:
    """Textual clamps a tree's scroll offset without moving its cursor, so the
    highlighted row silently leaves the window until the next keypress."""
    app = FNDApp(index_dir=built_index, config=tall_config)
    async with app.run_test(size=(120, 20)) as pilot:
        await pilot.pause()
        tree = await _deep_tree(app, pilot)
        assert tree.cursor_line > 3, "test setup — the panel must be long enough to scroll"
        tree.scroll_to(y=0, animate=False)
        for _ in range(4):
            await pilot.pause()
        assert not _visible(tree), "test setup — the cursor should be off screen"

        tree.post_message(Resize(tree.size, tree.container_size))
        for _ in range(20):
            await pilot.pause()
        assert _visible(tree), "a resize left the highlighted row off screen"


@pytest.mark.asyncio
async def test_the_re_search_restores_the_highlighted_row(
    built_index: Path, tall_config: Config
) -> None:
    """The scope toggle's own path, independent of any resize."""
    app = FNDApp(index_dir=built_index, config=tall_config)
    async with app.run_test(size=(120, 20)) as pilot:
        await pilot.pause()
        tree = await _deep_tree(app, pilot)
        assert tree.cursor_line > 3, "test setup — the panel must be long enough to scroll"
        tree.scroll_to(y=0, animate=False)
        for _ in range(4):
            await pilot.pause()
        assert not _visible(tree)

        app._scope._keep_scope_cursors_visible()
        for _ in range(10):
            await pilot.pause()
        assert _visible(tree), "the row was still stranded after the re-search"

"""The pinned Clear bar docked at the top of the filters pane container.

It floats above the scrolling tree so it stays visible whatever the tag list's
scroll — the reason a scrolling in-tree row was rejected.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Static, Tree

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
def idx(tmp_path: Path, tmp_index_dir: Path) -> Path:
    root = tmp_path / "papers"
    root.mkdir(parents=True, exist_ok=True)
    # Many tagged files so the tree scrolls — the case the pinned bar exists for.
    for i in range(40):
        (root / f"n{i}.md").write_text(
            f"---\ntags: [t{i}]\n---\n\n# N{i}\n\nsaffron\n", encoding="utf-8"
        )
    build_index(roots=[root], index_dir=tmp_index_dir, collection="papers")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_bar_hidden_when_no_filters(cfg: Config, idx: Path) -> None:
    app = FNDApp(index_dir=idx, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#clear_filters_bar", Static).display is False


@pytest.mark.asyncio
async def test_bar_appears_when_active(cfg: Config, idx: Path) -> None:
    app = FNDApp(index_dir=idx, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._scope.filter_kinds = ["md"]
        app._scope.refresh_filters_panel()
        await pilot.pause()
        bar = app.query_one("#clear_filters_bar", Static)
        assert bar.display is True
        assert "Clear" in str(bar.render())


@pytest.mark.asyncio
async def test_bar_docks_at_top_inside_the_pane(cfg: Config, idx: Path) -> None:
    """Inside the container's border, above the scrolling tree — so it floats
    in view regardless of scroll."""
    app = FNDApp(index_dir=idx, config=cfg)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        app._scope.filter_kinds = ["md"]
        app._scope.refresh_filters_panel()
        await pilot.pause()
        pane = app.query_one("#filters_pane")
        bar = app.query_one("#clear_filters_bar", Static)
        tree = app.query_one("#filters_panel_tree", Tree)
        assert pane.region.y < bar.region.y <= pane.region.y + 2  # just inside top border
        assert bar.region.y < tree.region.y  # above the tree


@pytest.mark.asyncio
async def test_bar_stays_visible_when_tree_scrolled(cfg: Config, idx: Path) -> None:
    """The whole point: scroll deep into the tag list, bar is still shown."""
    app = FNDApp(index_dir=idx, config=cfg)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        app._scope.filter_kinds = ["md"]
        app._scope.refresh_filters_panel()
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        tags = next(n for n in tree.root.children if "Tags" in str(n.label))
        tags.expand()
        await pilot.pause()
        tree.scroll_end(animate=False)
        await pilot.pause()
        assert app.query_one("#clear_filters_bar", Static).display is True


@pytest.mark.asyncio
async def test_bar_hides_again_when_cleared(cfg: Config, idx: Path) -> None:
    app = FNDApp(index_dir=idx, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._scope.filter_kinds = ["md"]
        app._scope.refresh_filters_panel()
        await pilot.pause()
        app._scope.clear_filters()
        await pilot.pause()
        assert app.query_one("#clear_filters_bar", Static).display is False


@pytest.mark.asyncio
async def test_clicking_the_bar_clears(cfg: Config, idx: Path) -> None:
    app = FNDApp(index_dir=idx, config=cfg)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        app._scope.filter_kinds = ["md"]
        app._scope.tag_include = {"frontmatter": {"t1"}}
        app._scope.refresh_filters_panel()
        await pilot.pause()
        await pilot.click("#clear_filters_bar")
        await pilot.pause()
        assert app._scope.has_active_filters is False
        assert app.query_one("#clear_filters_bar", Static).display is False


@pytest.mark.asyncio
async def test_no_clear_row_inside_the_tree(cfg: Config, idx: Path) -> None:
    app = FNDApp(index_dir=idx, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._scope.filter_kinds = ["md"]
        app._scope.refresh_filters_panel()
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        assert not any("Clear all filters" in str(n.label) for n in tree.root.children)


@pytest.mark.asyncio
async def test_filters_tree_has_bounded_height_so_it_scrolls(cfg: Config, idx: Path) -> None:
    """Regression: the tree inside the container must be a bounded (1fr) height
    so it SCROLLS. `height: auto` grew it to full content height and it was
    merely clipped by the container, stranding the cursor off-screen. (Actual
    scrolling can't be asserted headlessly — run_test doesn't resolve layout
    heights — so this guards the exact style that broke; scroll behaviour itself
    is verified in the tmux harness.)"""
    app = FNDApp(index_dir=idx, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        height = tree.styles.height
        assert height is not None
        assert not height.is_auto, "tree height is auto — it will clip instead of scroll"
        assert height.unit.name == "FRACTION", f"expected 1fr, got {height}"


@pytest.mark.asyncio
async def test_bar_shows_the_active_filter_count(cfg: Config, idx: Path) -> None:
    """Not a '(X)' key-hint that reads as a placeholder — the count of active
    filters."""
    app = FNDApp(index_dir=idx, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._scope.filter_kinds = ["md"]
        app._scope.tag_include = {"frontmatter": {"t1", "t2"}}
        app._scope.refresh_filters_panel()
        await pilot.pause()
        assert app._scope.active_filter_count == 3  # 1 kind + 2 tags
        text = str(app.query_one("#clear_filters_bar", Static).render())
        assert "Clear 3 filters" in text
        assert "(X)" not in text


@pytest.mark.asyncio
async def test_count_is_singular_for_one_filter(cfg: Config, idx: Path) -> None:
    app = FNDApp(index_dir=idx, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._scope.filter_date = "week"
        app._scope.refresh_filters_panel()
        await pilot.pause()
        assert "Clear 1 filter" in str(app.query_one("#clear_filters_bar", Static).render())


@pytest.mark.asyncio
async def test_bar_is_keyboard_selectable_from_the_tree_top(cfg: Config, idx: Path) -> None:
    """Up from the top filter row focuses the bar; Enter clears and returns
    focus to the tree. Fixes 'the clear button can't be selected'."""
    from fnd.tui.widgets.clear_bar import ClearFiltersBar

    app = FNDApp(index_dir=idx, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._scope.filter_kinds = ["md"]
        app._scope.refresh_filters_panel()
        await pilot.pause()

        tree = app.query_one("#filters_panel_tree", Tree)
        tree.focus()
        tree.cursor_line = 0
        await pilot.pause()
        await pilot.press("up")  # from the top row -> the bar
        await pilot.pause()
        assert isinstance(app.focused, ClearFiltersBar), f"focused {app.focused!r}"

        await pilot.press("enter")  # activate the selected bar
        await pilot.pause()
        assert app._scope.has_active_filters is False
        assert app.query_one("#clear_filters_bar", Static).display is False
        # Focus returned to the tree, not stranded on the vanished bar.
        assert app.focused is app.query_one("#filters_panel_tree", Tree)


@pytest.mark.asyncio
async def test_up_does_not_focus_bar_when_hidden(cfg: Config, idx: Path) -> None:
    """No active filter -> no bar -> Up at the top row is an ordinary no-op,
    focus stays on the tree."""
    app = FNDApp(index_dir=idx, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        tree.focus()
        tree.cursor_line = 0
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is tree

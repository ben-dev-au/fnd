"""Tags section of the filters pane, end to end against a real index."""

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
from tests._pilot_wait import run_search


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
def tagged_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    root = tmp_path / "papers"
    root.mkdir(parents=True, exist_ok=True)
    (root / "risotto.md").write_text(
        "---\ntags: [recipe, dinner]\n---\n\n# Risotto\n\nsaffron and stock\n", encoding="utf-8"
    )
    (root / "steak.md").write_text(
        "---\ntags: [recipe, project/alpha]\n---\n\n# Steak\n\nsaffron rub\n", encoding="utf-8"
    )
    (root / "trip.md").write_text(
        "---\ntags: [project/beta]\n---\n\n# Trip\n\nsaffron market\n", encoding="utf-8"
    )
    build_index(roots=[root], index_dir=tmp_index_dir, collection="papers")
    return tmp_index_dir


def _branch(tree: Tree[Any], label: str) -> TreeNode[Any]:
    for node in tree.root.children:
        if label in str(node.label):
            return node
    raise AssertionError(
        f"branch {label!r} not found in {[str(n.label) for n in tree.root.children]}"
    )


def _descend(node: TreeNode[Any], label: str) -> TreeNode[Any]:
    for child in node.children:
        if label in str(child.label):
            return child
    raise AssertionError(
        f"{label!r} not under {node.label!r}: {[str(c.label) for c in node.children]}"
    )


def _all_labels(node: TreeNode[Any]) -> list[str]:
    out = [str(node.label)]
    for c in node.children:
        out.extend(_all_labels(c))
    return out


@pytest.mark.asyncio
async def test_tags_branch_lists_indexed_tags(cfg: Config, tagged_index: Path) -> None:
    app = FNDApp(index_dir=tagged_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        tags = _branch(tree, "Tags")
        tags.expand()
        await pilot.pause()
        fm = _descend(tags, "Frontmatter")
        fm.expand()
        await pilot.pause()
        labels = " ".join(_all_labels(fm))
        assert "recipe" in labels
        assert "dinner" in labels
        assert "project" in labels


@pytest.mark.asyncio
async def test_counts_are_file_counts(cfg: Config, tagged_index: Path) -> None:
    app = FNDApp(index_dir=tagged_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        tags = _branch(tree, "Tags")
        tags.expand()
        await pilot.pause()
        fm = _descend(tags, "Frontmatter")
        fm.expand()
        await pilot.pause()
        recipe = _descend(fm, "recipe")
        assert "(2)" in str(recipe.label)  # risotto.md + steak.md


@pytest.mark.asyncio
async def test_enter_cycles_off_include_exclude(cfg: Config, tagged_index: Path) -> None:
    app = FNDApp(index_dir=tagged_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)

        def recipe_node() -> TreeNode[Any]:
            tags = _branch(tree, "Tags")
            tags.expand()
            fm = _descend(tags, "Frontmatter")
            fm.expand()
            return _descend(fm, "recipe")

        tree.select_node(recipe_node())
        await pilot.pause()
        assert "recipe" in app._scope.tag_include.get("frontmatter", set())

        tree.select_node(recipe_node())
        await pilot.pause()
        assert "recipe" in app._scope.tag_exclude.get("frontmatter", set())
        assert "recipe" not in app._scope.tag_include.get("frontmatter", set())

        tree.select_node(recipe_node())
        await pilot.pause()
        assert "recipe" not in app._scope.tag_include.get("frontmatter", set())
        assert "recipe" not in app._scope.tag_exclude.get("frontmatter", set())


@pytest.mark.asyncio
async def test_nested_tags_render_as_subtree(cfg: Config, tagged_index: Path) -> None:
    app = FNDApp(index_dir=tagged_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        tags = _branch(tree, "Tags")
        tags.expand()
        await pilot.pause()
        fm = _descend(tags, "Frontmatter")
        fm.expand()
        await pilot.pause()
        project = _descend(fm, "project")
        assert [str(c.label).split()[1] for c in project.children] == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_match_mode_row_toggles(cfg: Config, tagged_index: Path) -> None:
    app = FNDApp(index_dir=tagged_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        tags = _branch(tree, "Tags")
        tags.expand()
        await pilot.pause()
        assert app._scope.tag_match_all is True
        tree.select_node(_descend(_branch(tree, "Tags"), "Match:"))
        await pilot.pause()
        assert app._scope.tag_match_all is False


@pytest.mark.asyncio
async def test_selection_filters_the_results(cfg: Config, tagged_index: Path) -> None:
    """The real payoff: selecting a tag narrows the actual result set."""
    app = FNDApp(index_dir=tagged_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "saffron")
        before = {Path(g.path).name for g in app._search.groups}
        assert before == {"risotto.md", "steak.md", "trip.md"}

        app._scope.tag_include = {"frontmatter": {"recipe"}}
        await run_search(pilot, app, "saffron")
        after = {Path(g.path).name for g in app._search.groups}
        assert after == {"risotto.md", "steak.md"}


@pytest.mark.asyncio
async def test_exclusion_filters_the_results(cfg: Config, tagged_index: Path) -> None:
    app = FNDApp(index_dir=tagged_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._scope.tag_exclude = {"frontmatter": {"recipe"}}
        await run_search(pilot, app, "saffron")
        assert {Path(g.path).name for g in app._search.groups} == {"trip.md"}


@pytest.mark.asyncio
async def test_selecting_a_nested_parent_matches_its_children(
    cfg: Config, tagged_index: Path
) -> None:
    app = FNDApp(index_dir=tagged_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._scope.tag_include = {"frontmatter": {"project"}}
        await run_search(pilot, app, "saffron")
        assert {Path(g.path).name for g in app._search.groups} == {"steak.md", "trip.md"}


@pytest.mark.asyncio
async def test_selection_persists_across_restart(cfg: Config, tagged_index: Path) -> None:
    app = FNDApp(index_dir=tagged_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        tags = _branch(tree, "Tags")
        tags.expand()
        fm = _descend(tags, "Frontmatter")
        fm.expand()
        await pilot.pause()
        tree.select_node(_descend(fm, "dinner"))
        await pilot.pause()

    app2 = FNDApp(index_dir=tagged_index, config=cfg)
    async with app2.run_test() as pilot2:
        await pilot2.pause()
        assert "dinner" in app2._scope.tag_include.get("frontmatter", set())


@pytest.mark.asyncio
async def test_expanded_tag_parent_is_reachable_by_cursor(cfg: Config, tagged_index: Path) -> None:
    """Regression: the cursor skipped expanded parents, so Enter could never
    reach a nested tag whose subtree was open."""
    app = FNDApp(index_dir=tagged_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        tags = _branch(tree, "Tags")
        tags.expand()
        await pilot.pause()
        fm = _descend(tags, "Frontmatter")
        fm.expand()
        await pilot.pause()
        project = _descend(fm, "project")
        project.expand()
        await pilot.pause()

        tree.select_node(project)
        await pilot.pause()
        assert "project" in app._scope.tag_include.get("frontmatter", set())


@pytest.mark.asyncio
async def test_expanded_section_header_stays_under_cursor(cfg: Config, tagged_index: Path) -> None:
    """An expanded section header (File type) is reachable, not skipped.

    It was once treated as a dead row, but Enter/click on a section header now
    collapses the section — so the cursor must be able to land on it. Skipping
    it also caused a *click* on an expanded header to drift the cursor down
    onto (and toggle) its first child instead of collapsing the header.
    """
    app = FNDApp(index_dir=tagged_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        kinds = _branch(tree, "File type")
        kinds.expand()
        await pilot.pause()
        line = next(i for i, ln in enumerate(tree._tree_lines) if ln.node is kinds)
        tree.cursor_line = line
        await pilot.pause()
        # The cursor stays on the header (no drift onto its first child)...
        assert tree.cursor_node is kinds
        # ...and selecting it collapses the section.
        tree.select_node(kinds)
        await pilot.pause()
        assert not kinds.is_expanded


@pytest.mark.asyncio
async def test_border_title_reports_active_tags(cfg: Config, tagged_index: Path) -> None:
    """The panel header must show tags are filtering, like kinds and dates do."""
    app = FNDApp(index_dir=tagged_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        pane = app.query_one("#filters_pane")
        assert pane.border_title == "Filters"

        tags = _branch(tree, "Tags")
        tags.expand()
        fm = _descend(tags, "Frontmatter")
        fm.expand()
        await pilot.pause()
        tree.select_node(_descend(fm, "recipe"))
        await pilot.pause()
        assert "1 tag" in str(pane.border_title)

        # Second press moves it to excluded, which reads differently.
        tags = _branch(tree, "Tags")
        fm = _descend(tags, "Frontmatter")
        tree.select_node(_descend(fm, "recipe"))
        await pilot.pause()
        assert "−1 tag" in str(pane.border_title)


@pytest.mark.asyncio
async def test_toggling_a_tag_keeps_cursor_and_focus(cfg: Config, tagged_index: Path) -> None:
    """Toggling must not throw the user into the results pane.

    The pane rebuilds on every toggle (counts change), and the re-run search
    used to grab focus — between them the cursor left the tag being pressed.
    """
    app = FNDApp(index_dir=tagged_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "saffron")

        tree = app.query_one("#filters_panel_tree", Tree)
        tags = _branch(tree, "Tags")
        tags.expand()
        fm = _descend(tags, "Frontmatter")
        fm.expand()
        await pilot.pause()

        recipe = _descend(fm, "recipe")
        line = next(i for i, ln in enumerate(tree._tree_lines) if ln.node is recipe)
        tree.cursor_line = line
        tree.focus()
        await pilot.pause()

        tree.select_node(tree.cursor_node)
        await pilot.pause()

        assert app.focused is not None
        assert app.focused.id == "filters_panel_tree", "focus left the filters pane"
        node = tree.cursor_node
        assert node is not None
        assert "recipe" in str(node.label), f"cursor moved to {node.label!r}"
        assert "recipe" in app._scope.tag_include.get("frontmatter", set())


@pytest.mark.asyncio
async def test_tags_are_scoped_to_the_active_query(cfg: Config, tagged_index: Path) -> None:
    """Counts describe what the user is looking at, not the whole collection."""
    app = FNDApp(index_dir=tagged_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        # 'saffron' is in all three files; 'stock' only in risotto.md.
        await run_search(pilot, app, "saffron")
        wide = {t.value for t in app._scope.tag_catalogue_for_scope()["frontmatter"]}
        assert {"recipe", "dinner", "project"} <= wide

        await run_search(pilot, app, "stock")
        narrow = {t.value for t in app._scope.tag_catalogue_for_scope()["frontmatter"]}
        assert "dinner" in narrow
        assert "project" not in narrow, "tags still reflect files outside the result set"


@pytest.mark.asyncio
async def test_selecting_a_tag_does_not_hide_its_siblings(cfg: Config, tagged_index: Path) -> None:
    """The facet query must exclude the tag filter itself, or picking one tag
    strands the user with no way to switch to another."""
    app = FNDApp(index_dir=tagged_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "saffron")
        before = {t.value for t in app._scope.tag_catalogue_for_scope()["frontmatter"]}

        app._scope.tag_include = {"frontmatter": {"recipe"}}
        await run_search(pilot, app, "saffron")
        after = {t.value for t in app._scope.tag_catalogue_for_scope()["frontmatter"]}
        assert after == before, "siblings vanished once a tag was selected"


@pytest.fixture
def cfg_with_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [defaults]
            tag_frontmatter_keys = ["Course"]

            [[collections.papers.sources]]
            path = "/tmp/papers"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.fixture
def keyed_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    root = tmp_path / "papers"
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.md").write_text(
        "---\ntags: [project/alpha, solo]\nCourse: Algebra\n---\n\n# A\n\nsaffron\n",
        encoding="utf-8",
    )
    build_index(
        roots=[root],
        index_dir=tmp_index_dir,
        collection="papers",
        tag_frontmatter_keys=["Course"],
    )
    return tmp_index_dir


@pytest.mark.asyncio
async def test_frontmatter_key_namespace_is_not_selectable(
    cfg_with_keys: Config, keyed_index: Path
) -> None:
    """`course` names a field, not a tag — pressing Enter must do nothing."""
    app = FNDApp(index_dir=keyed_index, config=cfg_with_keys)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        tags = _branch(tree, "Tags")
        tags.expand()
        fm = _descend(tags, "Frontmatter")
        fm.expand()
        await pilot.pause()

        course = _descend(fm, "course")
        assert "○" not in str(course.label), "field header must carry no toggle marker"
        tree.select_node(course)
        await pilot.pause()
        assert app._scope.tag_include == {}
        assert app._scope.tag_exclude == {}

        # Its child is a real, selectable tag.
        course.expand()
        await pilot.pause()
        algebra = _descend(course, "algebra")
        tree.select_node(algebra)
        await pilot.pause()
        assert "course/algebra" in app._scope.tag_include.get("frontmatter", set())


@pytest.mark.asyncio
async def test_real_nested_tag_parent_stays_selectable(
    cfg_with_keys: Config, keyed_index: Path
) -> None:
    """`project` in `project/alpha` IS a tag the user wrote, unlike `course`."""
    app = FNDApp(index_dir=keyed_index, config=cfg_with_keys)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        tags = _branch(tree, "Tags")
        tags.expand()
        fm = _descend(tags, "Frontmatter")
        fm.expand()
        await pilot.pause()
        project = _descend(fm, "project")
        assert "○" in str(project.label)
        tree.select_node(project)
        await pilot.pause()
        assert "project" in app._scope.tag_include.get("frontmatter", set())


@pytest.mark.asyncio
async def test_leaf_markers_align_with_branch_markers(
    cfg_with_keys: Config, keyed_index: Path
) -> None:
    """A leaf sibling of a branch must not sit two columns to its left."""
    app = FNDApp(index_dir=keyed_index, config=cfg_with_keys)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        tags = _branch(tree, "Tags")
        tags.expand()
        fm = _descend(tags, "Frontmatter")
        fm.expand()
        await pilot.pause()
        branch = _descend(fm, "project")  # has children
        leaves = [c for c in fm.children if not c.children and "○" in str(c.label)]
        assert leaves, "expected at least one leaf tag"
        # Branch rows get Textual's 2-cell arrow; leaves pad to match.
        assert str(leaves[0].label).startswith("  "), str(leaves[0].label)
        assert not str(branch.label).startswith("  "), str(branch.label)

"""Excluded folders: a nested branch of the source's folders; ticking a parent covers its children."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from fnd.filters import FilterSpec
from fnd.filters.tree_model import (
    FolderOffer,
    apply_selection,
    selection_for,
    spec_branches,
)
from fnd.tui import FNDApp
from fnd.tui.widgets.toggle_tree import ToggleGroup, ToggleItem, ToggleTree
from fnd.walk import DirListing

LISTING = DirListing(dirs=("a", "b", "a/x", "a/y"), read=frozenset({"", "a", "b"}), truncated=False)


def _branch(spec: FilterSpec, offer: FolderOffer | None) -> Any:
    return next((b for b in spec_branches(spec, None, None, offer) if b.id == "folders"), None)


def test_a_global_screen_offers_no_folders() -> None:
    """No root, no branch and no typed row."""
    branches = spec_branches(FilterSpec())
    assert "folders" not in [b.id for b in branches]
    rules = next(b for b in branches if b.id == "rules")
    assert "rule:exclude_dirs" not in [i[0] for i in rules.items]


def test_the_branch_nests_the_listed_folders() -> None:
    """A folder with children is a group, a childless one a row; ids carry the path."""
    branch = _branch(FilterSpec(), FolderOffer(LISTING))
    assert [i[0] for i in branch.items] == ["dir:b"]
    group = branch.groups[0]
    assert group.id == "dir:a"
    assert [i[0] for i in group.items] == ["dir:a/x", "dir:a/y"]
    assert group.complete


def test_a_configured_folder_the_scan_missed_is_still_a_row_under_its_ancestors() -> None:
    """Typed or cut-off entries stay visible so they can be unticked."""
    spec = FilterSpec(exclude_dirs=("deep/er/still",))
    branch = _branch(spec, FolderOffer(LISTING))
    deep = next(g for g in branch.groups if g.id == "dir:deep")
    assert not deep.complete
    assert deep.groups[0].id == "dir:deep/er"
    assert deep.groups[0].items[0][0] == "dir:deep/er/still"


def test_a_branch_still_listing_says_so() -> None:
    """While the worker runs the branch exists, empty, marked incomplete."""
    branch = _branch(FilterSpec(), FolderOffer(None))
    assert branch.empty_label == "scanning…"
    assert not branch.complete


def test_a_partial_listing_says_so_on_the_branch() -> None:
    """A cut-short scan must not read as the whole tree."""
    cut = DirListing(dirs=("a",), read=frozenset(), truncated=True)
    branch = _branch(FilterSpec(), FolderOffer(cut))
    assert "partial list" in branch.label
    assert "partial list" not in _branch(FilterSpec(), FolderOffer(LISTING)).label


def test_the_legend_says_a_partly_excluded_folder_keeps_its_own_files() -> None:
    """Splitting an excluded parent frees the files directly inside it, which the rows cannot show."""
    from fnd.filters.tree_model import FOLDERS_LEGEND

    assert "its own files" in FOLDERS_LEGEND


def test_the_typed_row_lists_the_stored_paths() -> None:
    """Typed paths live with the other rules you type."""
    spec = FilterSpec(exclude_dirs=("a", "b/c"))
    branches = spec_branches(spec, None, None, FolderOffer(LISTING))
    rules = next(b for b in branches if b.id == "rules")
    row = next(i for i in rules.items if i[0] == "rule:exclude_dirs")
    assert row[1] == "Excluded folders (as paths)   a, b/c"


def test_the_selection_round_trips_and_keeps_the_order_written() -> None:
    """Ticking leaves the earlier entries where they were and appends the new one."""
    spec = FilterSpec(exclude_dirs=("z", "a"))
    selected, excluded = selection_for(spec)
    assert {"dir:z", "dir:a"} <= selected
    updated, _g, _f = apply_selection(spec, selected | {"dir:m"}, excluded)
    assert updated.exclude_dirs == ("z", "a", "m")
    back, _g, _f = apply_selection(updated, selected, excluded)
    assert back == spec


async def _settle(pilot: Any, n: int = 6) -> None:
    for _ in range(n):
        await pilot.pause()


def _walk(node: Any) -> Iterator[Any]:
    for child in node.children:
        yield child
        yield from _walk(child)


def _folders_group(complete: bool = True) -> ToggleGroup:
    a = ToggleGroup(
        "dir:a",
        "a",
        (ToggleItem("dir:a/x", "x"), ToggleItem("dir:a/y", "y")),
        mode="folders",
        complete=complete,
    )
    return ToggleGroup(
        "folders",
        "Excluded folders",
        (ToggleItem("dir:b", "b"),),
        mode="folders",
        groups=(a,),
        empty_label="none",
    )


def _label(tree: ToggleTree, item_id: str) -> str:
    node = next(n for n in _walk(tree.root) if (n.data or {}).get("id") == item_id)
    return str(node.label)


@pytest.mark.asyncio
async def test_ticking_a_parent_covers_its_children_and_rolls_up(tmp_index_dir: Any) -> None:
    """The parent and every child read on; the branch reads partly on."""
    tree = ToggleTree("Filters", id="t")
    async with FNDApp(index_dir=tmp_index_dir).run_test(size=(100, 30)) as pilot:
        await pilot.app.mount(tree)
        tree.set_model([_folders_group()], set(), expanded={"folders", "dir:a"})
        await _settle(pilot)
        tree._toggle(next(n for n in _walk(tree.root) if (n.data or {}).get("id") == "dir:a"))
        await _settle(pilot)
        assert tree.selected == {"dir:a"}
        assert "●" in _label(tree, "dir:a")
        assert "●" in _label(tree, "dir:a/x")
        assert "◐" in _label(tree, "folders")
        assert "●" not in _label(tree, "dir:b")


@pytest.mark.asyncio
async def test_unticking_a_covered_child_splits_the_parent_when_it_was_listed(
    tmp_index_dir: Any,
) -> None:
    """Only the child comes back; its sibling stays excluded."""
    tree = ToggleTree("Filters", id="t")
    async with FNDApp(index_dir=tmp_index_dir).run_test(size=(100, 30)) as pilot:
        await pilot.app.mount(tree)
        tree.set_model([_folders_group()], {"dir:a"}, expanded={"folders", "dir:a"})
        await _settle(pilot)
        tree._toggle(next(n for n in _walk(tree.root) if (n.data or {}).get("id") == "dir:a/x"))
        await _settle(pilot)
        assert tree.selected == {"dir:a/y"}


@pytest.mark.asyncio
async def test_unticking_a_covered_child_of_an_unlisted_parent_is_refused(
    tmp_index_dir: Any,
) -> None:
    """Nothing changes when a split would un-exclude folders the scan never saw."""
    tree = ToggleTree("Filters", id="t")
    async with FNDApp(index_dir=tmp_index_dir).run_test(size=(100, 30)) as pilot:
        await pilot.app.mount(tree)
        tree.set_model([_folders_group(complete=False)], {"dir:a"}, expanded={"folders", "dir:a"})
        await _settle(pilot)
        tree._toggle(next(n for n in _walk(tree.root) if (n.data or {}).get("id") == "dir:a/x"))
        await _settle(pilot)
        assert tree.selected == {"dir:a"}


@pytest.mark.asyncio
async def test_a_row_filter_does_not_hide_siblings_from_a_split(tmp_index_dir: Any) -> None:
    """Unticking the one row a filter shows still keeps its hidden siblings excluded."""
    full = _folders_group()
    shown = ToggleGroup(
        "folders",
        "Excluded folders",
        (),
        mode="folders",
        groups=(
            ToggleGroup("dir:a", "a", (ToggleItem("dir:a/x", "x"),), mode="folders", complete=True),
        ),
    )
    tree = ToggleTree("Filters", id="t")
    async with FNDApp(index_dir=tmp_index_dir).run_test(size=(100, 30)) as pilot:
        await pilot.app.mount(tree)
        tree.set_model([shown], {"dir:a"}, expanded={"folders", "dir:a"}, folder_model=[full])
        await _settle(pilot)
        tree._toggle(next(n for n in _walk(tree.root) if (n.data or {}).get("id") == "dir:a/x"))
        await _settle(pilot)
        assert tree.selected == {"dir:a/y"}

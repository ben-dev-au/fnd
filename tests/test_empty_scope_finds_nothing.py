"""An empty scope returns nothing, because that is what the panel says.

Unticking every collection paints `Collections · 0/5 active` and every row
`○`, so it must not return hits from all five: an empty selection collapsed to
None reads as "unscoped" to the query layer. PARTLY ticked collections are held
to the same honesty.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.config import CollectionConfig, SourceConfig
from fnd.index import build_index_from_config
from fnd.query import Searcher


@pytest.fixture
def two_collections(tmp_path: Path) -> Path:
    index_dir = tmp_path / "idx"
    for name in ("alpha", "beta"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / f"{name}.md").write_text(f"# {name}\n\nzebrafish\n", encoding="utf-8")
        build_index_from_config(
            config=CollectionConfig(sources=[SourceConfig(path=folder)]),
            collection=name,
            index_dir=index_dir,
        )
    return index_dir


def test_no_scope_at_all_searches_everything(two_collections: Path) -> None:
    """The control: None means unscoped, and the CLI relies on it."""
    hits = Searcher(index_dir=two_collections).search("zebrafish", collection=None)
    assert len(hits) == 2


def test_an_explicit_empty_scope_returns_nothing(two_collections: Path) -> None:
    hits = Searcher(index_dir=two_collections).search("zebrafish", collection=[])
    assert hits == []


def test_one_named_collection_still_narrows(two_collections: Path) -> None:
    hits = Searcher(index_dir=two_collections).search("zebrafish", collection=["alpha"])
    assert len(hits) == 1


def test_a_source_scope_without_a_collection_still_works(
    two_collections: Path, tmp_path: Path
) -> None:
    """The near-miss this fix could have caused: a PARTLY ticked collection
    contributes no collection name, and is scoped by source instead. Passing
    the empty list there would return nothing for every partial selection."""
    source = str((tmp_path / "alpha").resolve())
    hits = Searcher(index_dir=two_collections).search(
        "zebrafish", collection=None, source_scope={"alpha": [source]}
    )
    assert len(hits) == 1


def _config(tmp_path: Path):
    from fnd.config import Config

    return Config(
        collections={
            name: CollectionConfig(sources=[SourceConfig(path=tmp_path / name)])
            for name in ("alpha", "beta")
        }
    )


@pytest.mark.asyncio
async def test_an_app_with_nothing_to_scope_by_still_searches(two_collections: Path) -> None:
    """An empty selection map is not always the user unticking everything.

    An app with no config has no collections to tick; reading the two the same
    makes a fresh app find nothing.
    """
    from fnd.tui import FNDApp

    app = FNDApp(index_dir=two_collections)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        request = app._search._prepare("zebrafish")  # type: ignore[attr-defined]
        scope = request.collection if request is not None else "no request"

    assert scope is None, f"an app with nothing to scope by must not narrow: {scope!r}"


@pytest.mark.asyncio
async def test_unticking_every_collection_through_the_panel_finds_nothing(
    two_collections: Path, tmp_path: Path
) -> None:
    """Driven through the toggle the user actually presses.

    Both toggle paths POP their key, so unticking everything leaves the same
    empty map a launch has; a map set by hand passes either way while the panel
    reads `0/2 active` and the search returns both.
    """
    from textual.widgets import Tree

    from fnd.tui import FNDApp

    app = FNDApp(index_dir=two_collections, config=_config(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert app._scope.collections, "the premise: a fresh app has both ticked"
        tree = app.query_one("#collections_panel_tree", Tree)
        for node in list(tree.root.children):
            app._scope.on_collections_selected(Tree.NodeSelected(node))
            await pilot.pause()
        markers = {n: app._scope.collection_marker(n) for n in ("alpha", "beta")}
        request = app._search._prepare("zebrafish")  # type: ignore[attr-defined]
        scope = request.collection if request is not None else "no request"

    assert markers == {"alpha": "○", "beta": "○"}, markers
    assert scope == [], f"every row reads off and the search covered them all: {scope!r}"


@pytest.mark.asyncio
async def test_the_search_the_tui_actually_runs_honours_it(
    two_collections: Path, tmp_path: Path
) -> None:
    """End to end, because only the search the TUI runs shows the defect.

    `query.py` returns nothing for an explicit empty list, but the cascade (the
    pass that recovers a sparse query) must not test `if collection:`: an empty
    list is falsy, so it would skip the collection filter and answer from every
    collection under a panel reading `0/2 active`.
    """
    from textual.widgets import Tree

    from fnd.tui import FNDApp

    app = FNDApp(index_dir=two_collections, config=_config(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        for _ in range(15):
            await pilot.pause()
        assert app._scope.collections, "the premise: a fresh app has both ticked"
        tree = app.query_one("#collections_panel_tree", Tree)
        for node in list(tree.root.children):
            app._scope.on_collections_selected(Tree.NodeSelected(node))
            await pilot.pause()
        app._search.run("zebrafish")
        for _ in range(300):
            await pilot.pause()
            if app._search.idle:
                break
        groups = len(app._search.groups)
        title = str(app.query_one("#results_pane", Tree).border_title)

    assert groups == 0, f"every row reads off and the search returned {groups} files"
    assert "nothing matched" in title, title


def test_the_recovery_pass_honours_an_empty_scope_too(two_collections: Path) -> None:
    """The unit under the end-to-end one: the cascade's own collection gate."""
    from fnd.cascade import _fuzzy_pass
    from fnd.query import Searcher

    searcher = Searcher(index_dir=two_collections)
    hits = _fuzzy_pass(searcher, query="zebrafsh", limit=10, collection=[])

    assert hits == [], f"an empty scope let the recovery pass answer: {len(hits)} hits"

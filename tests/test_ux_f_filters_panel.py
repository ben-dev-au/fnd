"""UX-F: Filters panel — third left-column panel.

File-type filter (multi-select) and Date filter (single-select radio)
compose into the user's query as DSL clauses (``kind:(pdf md)``,
``mtime:week``). The panel mirrors the Collections panel: tree widget,
border title with active count, ●/○ markers, Enter to toggle, Left to
collapse-to-header. Selections persist to ``scope.toml``.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Tree

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp


def _write_md(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture
def cfg_one_collection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
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
def mixed_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """Index a small md+txt corpus so kind: filtering has something to bite on."""
    a = tmp_path / "papers"
    _write_md(a / "a.md", "# A\nshared anchor: glimmer\n")
    _write_md(a / "b.txt", "shared anchor: glimmer\n")
    build_index(roots=[a], index_dir=tmp_index_dir, collection="papers")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_filters_panel_mounts(cfg_one_collection: Config, mixed_index: Path) -> None:
    """The Filters panel is a third Tree widget visible at startup."""
    app = FNDApp(index_dir=mixed_index, config=cfg_one_collection)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        labels = [str(c.label) for c in tree.root.children]
        joined = "\n".join(labels)
        assert "File type" in joined, joined
        # "Modified" reads more clearly than "Date" — the underlying
        # field is the file's mtime, not creation/published date.
        assert "Modified" in joined, joined


@pytest.mark.asyncio
async def test_filters_panel_header_shows_state(
    cfg_one_collection: Config, mixed_index: Path
) -> None:
    """Border title summarises active filters at a glance."""
    app = FNDApp(index_dir=mixed_index, config=cfg_one_collection)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        title = str(tree.border_title or "")
        # No filters active → title says "Filters" with no qualifier.
        assert title.startswith("Filters"), title


@pytest.mark.asyncio
async def test_kind_toggle_is_multi_select(cfg_one_collection: Config, mixed_index: Path) -> None:
    """Selecting two file kinds keeps both active (multi-select)."""
    app = FNDApp(index_dir=mixed_index, config=cfg_one_collection)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        # Expand the File type category and select two children.
        kind_node = next(c for c in tree.root.children if "File type" in str(c.label))
        kind_node.expand()
        await pilot.pause()
        # Toggle pdf and md by walking the tree to those leaves.
        pdf_leaf = next(c for c in kind_node.children if " pdf" in str(c.label))
        md_leaf = next(c for c in kind_node.children if " md" in str(c.label))
        tree.focus()
        tree.select_node(pdf_leaf)
        await pilot.pause()
        tree.select_node(md_leaf)
        await pilot.pause()
        assert sorted(app._scope.filter_kinds) == ["md", "pdf"]


@pytest.mark.asyncio
async def test_date_toggle_is_single_select(cfg_one_collection: Config, mixed_index: Path) -> None:
    """Selecting a date option replaces the previous selection."""
    app = FNDApp(index_dir=mixed_index, config=cfg_one_collection)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        date_node = next(c for c in tree.root.children if "Modified" in str(c.label))
        date_node.expand()
        await pilot.pause()
        week_leaf = next(c for c in date_node.children if " week" in str(c.label))
        month_leaf = next(c for c in date_node.children if " month" in str(c.label))
        tree.focus()
        tree.select_node(week_leaf)
        await pilot.pause()
        assert app._scope.filter_date == "week"
        tree.select_node(month_leaf)
        await pilot.pause()
        # Single-select: month replaces week, not appended.
        assert app._scope.filter_date == "month"


@pytest.mark.asyncio
async def test_filters_compose_into_query(cfg_one_collection: Config, mixed_index: Path) -> None:
    """Active filters get AND-combined with the lexical query before
    each fusion sub-query reaches the searcher. We spy at the lowest
    layer (``_filtered_raw_hits``) since fusion issues multiple parallel
    sub-queries — at least one of them must carry the kind/date filter
    clauses for the field-restriction to take effect."""
    app = FNDApp(index_dir=mixed_index, config=cfg_one_collection)
    async with app.run_test() as pilot:
        await pilot.pause()
        captured_queries: list[str] = []
        searcher = app._search.searcher
        assert searcher is not None
        original = searcher._filtered_raw_hits

        def spy(query: str, **kwargs: object) -> list[object]:
            captured_queries.append(query)
            return original(query, **kwargs)  # type: ignore[no-any-return,arg-type]

        searcher._filtered_raw_hits = spy  # type: ignore[method-assign]
        # Activate kind=md filter.
        app._scope.filter_kinds = ["md"]
        app._scope.filter_date = "week"
        app._search.run("glimmer")
        await pilot.pause()
        joined = " || ".join(captured_queries)
        assert "kind:md" in joined, joined
        assert "mtime:week" in joined, joined
        assert "glimmer" in joined, joined


@pytest.mark.asyncio
async def test_kind_multi_select_uses_or_group(
    cfg_one_collection: Config, mixed_index: Path
) -> None:
    """Multiple kinds compose as ``kind:(a b)`` so Tantivy treats them
    as a disjunction across the kind field."""
    app = FNDApp(index_dir=mixed_index, config=cfg_one_collection)
    async with app.run_test() as pilot:
        await pilot.pause()
        captured_queries: list[str] = []
        searcher = app._search.searcher
        assert searcher is not None
        original = searcher._filtered_raw_hits

        def spy(query: str, **kwargs: object) -> list[object]:
            captured_queries.append(query)
            return original(query, **kwargs)  # type: ignore[no-any-return,arg-type]

        searcher._filtered_raw_hits = spy  # type: ignore[method-assign]
        app._scope.filter_kinds = ["pdf", "md"]
        app._search.run("glimmer")
        await pilot.pause()
        joined = " || ".join(captured_queries)
        # Order-independent check across all sub-queries.
        assert "kind:(" in joined, joined
        assert "pdf" in joined
        assert "md" in joined


@pytest.mark.asyncio
async def test_filters_persist_across_restart(
    cfg_one_collection: Config, mixed_index: Path
) -> None:
    """Toggling a filter writes to scope.toml so the next launch restores it."""
    app = FNDApp(index_dir=mixed_index, config=cfg_one_collection)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._scope.filter_kinds = ["pdf"]
        app._scope.filter_date = "week"
        app._scope.persist()
        await pilot.pause()
    # Fresh app reads the same state file (autouse fixture isolates path).
    app2 = FNDApp(index_dir=mixed_index, config=cfg_one_collection)
    assert app2._scope.filter_kinds == ["pdf"]
    assert app2._scope.filter_date == "week"


@pytest.mark.asyncio
async def test_active_kind_marked_in_label(cfg_one_collection: Config, mixed_index: Path) -> None:
    """Selected file-type leaves show the ● marker; unselected show ○."""
    app = FNDApp(index_dir=mixed_index, config=cfg_one_collection)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._scope.filter_kinds = ["pdf"]
        app._scope.refresh_filters_panel()
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        kind_node = next(c for c in tree.root.children if "File type" in str(c.label))
        kind_node.expand()
        await pilot.pause()
        for leaf in kind_node.children:
            label = str(leaf.label)
            if " pdf" in label:
                assert "●" in label, f"active pdf should be marked: {label!r}"
            else:
                assert "○" in label, f"inactive should show ○: {label!r}"

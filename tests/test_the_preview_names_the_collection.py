"""With several collections searched, the preview's bottom edge names the result's own."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from rich.style import Style
from textual.content import Content
from textual.widgets import Tree

from fnd.config import Config, load
from fnd.index import build_index_from_config
from fnd.query import Searcher
from fnd.tui import FNDApp
from fnd.tui.preview_edge import EDGE_RESERVED
from fnd.tui.preview_scrollbar import MatchAwareScroll
from fnd.tui.widgets.results_tree import ResultsTree
from tests._pilot_wait import wait_until

LONG = "departmental-meeting-minutes-archive"


@pytest.fixture
def shared_vault(tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    vault, work, home = tmp_path / "Vault", tmp_path / "WorkDocs", tmp_path / "HomeDocs"
    for d in (vault, work, home):
        d.mkdir()
        (d / f"{d.name.lower()}.md").write_text("# Note\n\nhaystack here.\n", encoding="utf-8")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.Work.sources]]
            path = "{vault.as_posix()}"
            [[collections.Work.sources]]
            path = "{work.as_posix()}"
            [[collections.{LONG}.sources]]
            path = "{vault.as_posix()}"
            [[collections.{LONG}.sources]]
            path = "{home.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    cfg = load(cfg_path)
    for name in ("Work", LONG):
        build_index_from_config(
            config=cfg.collections[name], collection=name, index_dir=tmp_index_dir
        )
    return cfg


def test_a_hit_carries_every_collection_its_file_is_in(
    shared_vault: Config, tmp_index_dir: Path
) -> None:
    hits = Searcher(index_dir=tmp_index_dir).search("haystack", limit=10)
    by_file = {Path(h.path).name: {c for c, _ in h.memberships} for h in hits}
    assert by_file == {"vault.md": {"Work", LONG}, "workdocs.md": {"Work"}, "homedocs.md": {LONG}}


def _edge(app: FNDApp) -> str:
    return Content.from_markup(
        app.query_one("#preview_pane", MatchAwareScroll).border_subtitle or ""
    ).plain


async def _edges_by_file(app: FNDApp, pilot: object) -> dict[str, str]:
    tree = app.query_one("#results_pane", ResultsTree)
    await wait_until(pilot, lambda: len(tree.root.children) == 3, message="three results")  # type: ignore[arg-type]
    tree.focus()
    out: dict[str, str] = {}
    for node in list(tree.root.children):
        name = Path(node.data["group"].path).name  # type: ignore[index]
        tree.move_cursor(node)
        await wait_until(pilot, lambda: name in app._preview_title(), message=name)  # type: ignore[arg-type]
        out[name] = _edge(app)
    return out


@pytest.mark.asyncio
async def test_each_result_names_its_collections(shared_vault: Config, tmp_index_dir: Path) -> None:
    app = FNDApp(
        index_dir=tmp_index_dir, config=shared_vault, collection="all", initial_query="haystack"
    )
    async with app.run_test(size=(200, 30)) as pilot:
        edges = await _edges_by_file(app, pilot)
    assert edges == {"vault.md": f"Work, {LONG}", "workdocs.md": "Work", "homedocs.md": LONG}


@pytest.mark.asyncio
async def test_one_collection_names_nothing(shared_vault: Config, tmp_index_dir: Path) -> None:
    app = FNDApp(
        index_dir=tmp_index_dir, config=shared_vault, collection="Work", initial_query="haystack"
    )
    async with app.run_test(size=(200, 30)) as pilot:
        tree = app.query_one("#results_pane", ResultsTree)
        await wait_until(pilot, lambda: len(tree.root.children) == 2, message="two results")
        await wait_until(pilot, lambda: ".md" in app._preview_title(), message="a preview")
        assert _edge(app) == ""


@pytest.mark.asyncio
async def test_the_edge_refits_when_the_terminal_resizes(
    shared_vault: Config, tmp_index_dir: Path
) -> None:
    app = FNDApp(
        index_dir=tmp_index_dir, config=shared_vault, collection="all", initial_query="haystack"
    )
    async with app.run_test(size=(200, 30)) as pilot:
        pane = app.query_one("#preview_pane", MatchAwareScroll)
        tree = app.query_one("#results_pane", ResultsTree)
        await wait_until(pilot, lambda: len(tree.root.children) == 3, message="three results")
        tree.focus()
        tree.move_cursor(
            next(n for n in tree.root.children if n.data["group"].path.endswith("homedocs.md"))  # type: ignore[index]
        )
        await wait_until(pilot, lambda: LONG in _edge(app), message="full name at 200")

        def fitted() -> bool:
            edge = _edge(app)
            return bool(edge) and len(edge) <= pane.outer_size.width - EDGE_RESERVED

        await pilot.resize_terminal(60, 30)
        await wait_until(pilot, lambda: pane.outer_size.width < 50, message="pane shrank")
        await wait_until(pilot, lambda: fitted() and "…" in _edge(app), message="name shortened")
        await pilot.resize_terminal(200, 30)
        await wait_until(pilot, lambda: pane.outer_size.width > 100, message="pane grew")
        await wait_until(pilot, lambda: LONG in _edge(app), message="full name again")


def _colours(label: object) -> str:
    return " ".join(str(span.style) for span in label.spans)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_unticking_a_source_keeps_the_marks_true(
    shared_vault: Config, tmp_index_dir: Path
) -> None:
    app = FNDApp(
        index_dir=tmp_index_dir, config=shared_vault, collection="all", initial_query="haystack"
    )
    async with app.run_test(size=(200, 40)) as pilot:
        results = app.query_one("#results_pane", ResultsTree)
        await wait_until(pilot, lambda: len(results.root.children) == 3, message="three results")
        panel = app.query_one("#collections_panel_tree", Tree)
        long_row = next(n for n in panel.root.children if n.data["name"] == LONG)  # type: ignore[index]
        long_row.expand()
        await wait_until(pilot, lambda: bool(long_row.children), message="sources listed")
        vault_source = next(
            n
            for n in long_row.children
            if n.data["source_id"].endswith("Vault")  # type: ignore[index]
        )
        panel.focus()

        def on_the_source() -> bool:
            if vault_source.line >= 0:
                panel.cursor_line = vault_source.line
            return panel.cursor_node is vault_source

        await wait_until(pilot, on_the_source, message="on the source")
        await pilot.press("enter")
        await wait_until(pilot, lambda: LONG in app._scope.source_scope, message="partial")

        marks = app._scope.collection_marks
        long_mark, work_mark = marks.mark(LONG), marks.mark("Work")
        assert long_mark is not None
        assert work_mark is not None
        assert str(long_row.label).startswith("◐")
        assert long_mark.colour in _colours(long_row.label)

        def vault_row_wears_work() -> bool:
            row = next(
                (n for n in results.root.children if n.data["group"].path.endswith("vault.md")),  # type: ignore[index]
                None,
            )
            return row is not None and work_mark.colour in _colours(row.label)

        await wait_until(pilot, vault_row_wears_work, message="vault.md marked as Work's alone")


@pytest.mark.asyncio
async def test_an_unfocused_cursor_row_keeps_its_mark(
    shared_vault: Config, tmp_index_dir: Path
) -> None:
    app = FNDApp(
        index_dir=tmp_index_dir, config=shared_vault, collection="all", initial_query="haystack"
    )
    async with app.run_test(size=(200, 40)) as pilot:
        results = app.query_one("#results_pane", ResultsTree)
        await wait_until(pilot, lambda: len(results.root.children) == 3, message="three results")
        panel = app.query_one("#collections_panel_tree", Tree)
        long_row = next(n for n in panel.root.children if n.data["name"] == LONG)  # type: ignore[index]
        home_row = next(
            n
            for n in results.root.children
            if n.data["group"].path.endswith("homedocs.md")  # type: ignore[index]
        )
        mark = app._scope.collection_marks.mark(LONG)
        assert mark is not None

        def painted(tree: Tree[Any], node: Any, needle: str) -> str:
            cursor = tree.get_component_rich_style("tree--cursor", partial=False)
            text = tree.render_label(node, Style(), cursor)
            colour = text.get_style_at_offset(Console(), text.plain.index(needle)).color
            return colour.triplet.hex if colour and colour.triplet else ""

        for tree, node, needle in ((panel, long_row, LONG[:4]), (results, home_row, ".md")):
            tree.focus()
            await wait_until(pilot, lambda t=tree: t.has_focus, message="focused")
            assert painted(tree, node, needle) != mark.colour
            app.query_one("#query_bar").focus()
            await wait_until(pilot, lambda t=tree: not t.has_focus, message="blurred")
            assert painted(tree, node, needle) == mark.colour


@pytest.fixture
def three_collections(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> Config:
    lines: list[str] = []
    for name in ("Alpha", "Beta", "Gamma"):
        root = tmp_path / name
        root.mkdir()
        (root / f"{name.lower()}.md").write_text("# Note\n\nhaystack here.\n", encoding="utf-8")
        lines += [f"[[collections.{name}.sources]]", f'path = "{root.as_posix()}"']
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    cfg = load(cfg_path)
    for name in ("Alpha", "Beta", "Gamma"):
        build_index_from_config(
            config=cfg.collections[name], collection=name, index_dir=tmp_index_dir
        )
    return cfg


def _hex_at(label: Any, needle: str) -> str:
    colour = label.get_style_at_offset(Console(), label.plain.index(needle)).color
    return colour.triplet.hex if colour and colour.triplet else ""


@pytest.mark.asyncio
async def test_a_batched_toggle_moves_row_marks_with_the_legend(
    three_collections: Config, tmp_index_dir: Path
) -> None:
    app = FNDApp(
        index_dir=tmp_index_dir,
        config=three_collections,
        collection="all",
        initial_query="haystack",
    )
    async with app.run_test(size=(200, 40)) as pilot:
        results = app.query_one("#results_pane", ResultsTree)
        await wait_until(pilot, lambda: len(results.root.children) == 3, message="three results")
        panel = app.query_one("#collections_panel_tree", Tree)
        rows = {n.data["name"]: n for n in panel.root.children}  # type: ignore[index]

        def on_alpha() -> bool:
            panel.focus()
            panel.cursor_line = rows["Alpha"].line
            return app._focus_context() == "collections" and panel.cursor_node is rows["Alpha"]

        await wait_until(pilot, on_alpha, message="cursor on Alpha")
        app.action_scope_toggle_batch()
        await wait_until(pilot, lambda: "Alpha" not in app._scope.collections, message="Alpha off")

        def row(name: str) -> Any:
            return next(
                n.label
                for n in results.root.children
                if n.data["group"].path.endswith(name)  # type: ignore[index]
            )

        beta_legend = _hex_at(rows["Beta"].label, "Beta")
        assert beta_legend
        assert _hex_at(row("beta.md"), ".md") == beta_legend
        assert _hex_at(row("alpha.md"), ".md") != beta_legend
        assert len(results.root.children) == 3

"""Section rows get their snippet when their file is open, anchored where the preview paints."""

from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from textual.widgets import Tree

from fnd.config import load
from fnd.index import build_index
from fnd.tui import FNDApp


def _sections(tree: Tree[Any], file_index: int) -> list[Any]:
    return list(tree.root.children[file_index].children)


def _shows_snippet(leaf: Any) -> bool:
    return leaf.data["hit"].materialised and "saffron" in leaf.label.plain.lower()


@pytest.mark.asyncio
async def test_an_expanded_file_fills_in_its_rows(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    for name in ("alpha", "beta"):
        parts = "\n\n".join(f"## S{i}\n\nsaffron harvest {i}." for i in range(3))
        (root / f"{name}.md").write_text(f"# {name}\n\n{parts}\n", encoding="utf-8")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.notes.sources]]
            path = "{root.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    monkeypatch.setattr("fnd.tui.results_view.UPFRONT_SECTIONS", 1)
    monkeypatch.setattr("fnd.tui.results_view._FILL_DEFER_S", 0.0)
    build_index(roots=[root], index_dir=tmp_index_dir, collection="notes")

    app = FNDApp(
        index_dir=tmp_index_dir, config=load(cfg_path), collection="notes", initial_query="saffron"
    )
    async with app.run_test(size=(120, 30)) as pilot:
        tree = app.query_one("#results_pane", Tree)
        for _ in range(60):
            await pilot.pause(0.05)
            if len(tree.root.children) == 2 and all(map(_shows_snippet, _sections(tree, 0))):
                break
        assert all(map(_shows_snippet, _sections(tree, 0))), "the open file's rows stayed bare"
        closed = _sections(tree, 1)
        assert not any(map(_shows_snippet, closed)), "a closed file paid for its snippets"

        tree.root.children[1].expand()
        for _ in range(60):
            await pilot.pause(0.05)
            if all(map(_shows_snippet, _sections(tree, 1))):
                break

        assert all(map(_shows_snippet, _sections(tree, 1)))
        assert all(leaf.data["hit"].materialised for leaf in _sections(tree, 1))


@pytest.mark.asyncio
async def test_a_synonym_only_row_anchors_on_the_synonym(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row snippet is built with the spec the preview paints, synonyms included."""
    root = tmp_path / "notes"
    root.mkdir()
    lead = " ".join(["preamble"] * 80)
    (root / "auth.md").write_text(
        f"# Auth\n\n{lead} multi-factor authentication stops replay.\n", encoding="utf-8"
    )
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.notes.sources]]
            path = "{root.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    build_index(roots=[root], index_dir=tmp_index_dir, collection="notes")

    app = FNDApp(
        index_dir=tmp_index_dir, config=load(cfg_path), collection="notes", initial_query="MFA"
    )
    async with app.run_test(size=(120, 30)) as pilot:
        tree = app.query_one("#results_pane", Tree)
        for _ in range(60):
            await pilot.pause(0.05)
            if tree.root.children and tree.root.children[0].children:
                break
        data = tree.root.children[0].children[0].data
        assert data is not None
        snippet = data["hit"].snippet

    assert "multi-factor" in snippet, snippet


def test_a_fill_for_replaced_results_is_dropped() -> None:
    """Rows materialised for an earlier search never land in the current one."""
    from fnd.query import FileGroup, Hit
    from fnd.tui.results_view import ResultsView

    light = Hit(
        score=1.0,
        parent_id="p",
        path="/a.md",
        kind="md",
        page=0,
        slide=0,
        heading_path="",
        title="",
        snippet="",
        materialised=False,
    )
    group = FileGroup(parent_id="p", path="/a.md", kind="md", title="", top_score=1.0, hits=[light])
    app = SimpleNamespace(_search=SimpleNamespace(groups=[group]))
    done = [
        Hit(
            score=1.0,
            parent_id="p",
            path="/a.md",
            kind="md",
            page=0,
            slide=0,
            heading_path="",
            title="",
            snippet="stale",
        )
    ]

    ResultsView(app)._apply_materialised([group], SimpleNamespace(data={"group": group}), done)  # type: ignore[arg-type]

    assert group.hits == [light]
    assert not group.hits[0].materialised

"""TUI shell — keyboard-driven flows via Pilot."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fnd.index import build_index
from fnd.tui import FNDApp


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_empty_query_shows_placeholder(built_index: Path) -> None:
    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Static

        preview = app.query_one("#placeholder", Static)
        assert preview is not None


@pytest.mark.asyncio
async def test_query_populates_results_tree(built_index: Path) -> None:
    """Type a phrase, submit, and verify the tree fills with file nodes."""
    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Input, Tree

        inp = app.query_one("#query_bar", Input)
        inp.value = "blue penguin sandwich"
        await pilot.press("enter")
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        # Tree.root has children = file nodes.
        children = list(tree.root.children)
        assert children, "expected at least one file node"
        first_label = str(children[0].label)
        assert "test.pdf" in first_label, f"top file was {first_label!r}"


@pytest.mark.asyncio
async def test_initial_query_seeds_results(built_index: Path) -> None:
    """Launching with --query should auto-populate the tree."""
    app = FNDApp(index_dir=built_index, initial_query="lavender stapler")
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Tree

        tree = app.query_one("#results_pane", Tree)
        children = list(tree.root.children)
        assert children
        assert "deck.pptx" in str(children[0].label)


@pytest.mark.asyncio
async def test_expanding_file_node_shows_section_hits(built_index: Path) -> None:
    app = FNDApp(index_dir=built_index, initial_query="page")
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Tree

        tree = app.query_one("#results_pane", Tree)
        children = list(tree.root.children)
        assert children
        # First file should have multiple section children once we expand it.
        first = children[0]
        first.expand()
        await pilot.pause()
        section_children = list(first.children)
        assert len(section_children) >= 2, f"got {len(section_children)} sections"


@pytest.mark.asyncio
async def test_o_key_opens_at_locator_on_focused_section(
    built_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enter no longer opens externally — only the explicit
    `o` key (action_open_at_locator) does."""
    calls: list[dict[str, Any]] = []

    def fake_open_smart(*, path: Path, kind: str, page: int = 0, **_kw: Any) -> int:
        calls.append({"path": str(path), "kind": kind, "page": page})
        return 0

    from fnd import opener

    monkeypatch.setattr(opener, "open_smart", fake_open_smart)

    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Tree

        tree = app.query_one("#results_pane", Tree)
        first = next(iter(tree.root.children))
        first.expand()
        await pilot.pause()
        tree.focus()
        await pilot.press("down")
        # Pressing Enter should NOT open the external app any more.
        await pilot.press("enter")
        await pilot.pause()
        assert not calls, f"Enter should not call open_smart; got {calls}"

        # Now `o` triggers action_open_at_locator → open_smart.
        app.action_open_at_locator()
        await pilot.pause()
        assert calls, "expected open_smart to be called via `o`"
        assert calls[-1]["kind"] == "pdf"
        assert calls[-1]["page"] == 7
        assert calls[-1]["path"].endswith("test.pdf")

"""Esc cascade — from any non-results pane Esc focuses the results tree.

The Settings menu lives on the screen stack with its own Esc handler;
this test exercises only the main-app cascade. Esc inside the menu is
covered by tests/test_actions_keymap.py and tests/test_phase_5_6_polish.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from acorn.index import build_index
from acorn.tui import AcornApp


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_escape_from_query_focuses_results(built_index: Path) -> None:
    app = AcornApp(index_dir=built_index, initial_query="orange penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#query_bar").focus()
        await pilot.pause()
        assert app._focus_context() == "query"
        await pilot.press("escape")
        await pilot.pause()
        assert app._focus_context() in {"results", "global"}


@pytest.mark.asyncio
async def test_escape_from_collections_panel_focuses_results(built_index: Path) -> None:
    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#collections_panel_tree").focus()
        await pilot.pause()
        assert app._focus_context() == "collections"
        await pilot.press("escape")
        await pilot.pause()
        assert app._focus_context() in {"results", "global"}


@pytest.mark.asyncio
async def test_escape_from_results_is_a_noop(built_index: Path) -> None:
    app = AcornApp(index_dir=built_index, initial_query="orange penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        # The CLI's initial-query path already focuses results; double-check.
        app.query_one("#results_pane").focus()
        await pilot.pause()
        before = app._focus_context()
        await pilot.press("escape")
        await pilot.pause()
        assert app._focus_context() == before

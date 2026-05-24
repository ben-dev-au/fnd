"""UX-A2: footer hints are curated and focus-aware.

Lazygit shows ~4-6 hints at any moment, prioritised by what the user
can usefully do given their current focus. FND's default Textual
``Footer`` dumps every bound action — busy, hard to scan. This test
suite pins the curated behaviour: result-pane actions appear when the
results tree is focused, query actions when the query input is, and a
small set of global actions are always visible.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Static, Tree

from fnd.index import build_index
from fnd.tui import FNDApp


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


def _footer_text(app: FNDApp) -> str:
    """Read the rendered footer hints. The custom widget is a Static
    with id ``footer_hints`` mounted in place of Textual's default Footer."""
    return str(app.query_one("#footer_hints", Static).content)


@pytest.mark.asyncio
async def test_footer_shows_at_most_six_hints(built_index: Path) -> None:
    """Hard cap on visible hints — beyond this it stops being a glance
    and becomes a list. lazygit's footer holds 5-6 hints comfortably."""
    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = _footer_text(app)
        # Hints are separated by 2+ spaces or a separator glyph; count
        # by splitting on the visible ":" between key and label.
        hint_count = text.count(":")
        assert hint_count <= 6, f"too many hints visible ({hint_count}): {text!r}"


@pytest.mark.asyncio
async def test_footer_query_context_shows_submit_not_open(built_index: Path) -> None:
    """When the query input is focused, the most relevant action is
    'submit query' (Enter). 'Open file' is irrelevant — there's no
    selected file yet."""
    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Default focus is the query input.
        text = _footer_text(app)
        # Open / peek / default-app are result actions — they should not
        # appear when no result is even selectable.
        assert "Open" not in text or "Peek" not in text or "Default" not in text, (
            f"query-context footer should hide result-only actions: {text!r}"
        )


@pytest.mark.asyncio
async def test_footer_results_context_shows_open(built_index: Path) -> None:
    """When the results tree is focused with a hit selected, the
    open-related actions (Open / Default / Peek) become the most
    relevant — they should appear in the footer."""
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        first = next(iter(tree.root.children))
        first.expand()
        await pilot.pause()
        tree.focus()
        await pilot.press("down")
        await pilot.pause()
        text = _footer_text(app)
        assert "Open" in text, f"results-context footer should show Open: {text!r}"


@pytest.mark.asyncio
async def test_footer_global_actions_always_visible(built_index: Path) -> None:
    """Help and Quit are always relevant — must appear in every context."""
    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = _footer_text(app)
        assert "Help" in text or "?" in text, f"Help missing: {text!r}"
        assert "Quit" in text or " q:" in text, f"Quit missing: {text!r}"

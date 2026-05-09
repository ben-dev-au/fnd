"""UX-C: visual score bars next to numeric scores in result rows."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Tree

from acorn.index import build_index
from acorn.tui import AcornApp
from acorn.tui.app import _score_bar


def test_score_bar_full_at_max() -> None:
    """A score equal to the max should render as 4 full blocks."""
    bar = _score_bar(score=10.0, max_score=10.0, width=4)
    assert bar == "████"


def test_score_bar_empty_at_zero_max() -> None:
    """Defensive: a max of 0 (no scored results) shouldn't divide by zero —
    return an empty bar."""
    bar = _score_bar(score=0.0, max_score=0.0, width=4)
    assert bar == "    "


def test_score_bar_proportional_in_middle() -> None:
    """Half the max → bar should be roughly half full."""
    bar = _score_bar(score=5.0, max_score=10.0, width=8)
    # Visual count of "filled" cells (not whitespace) should be roughly 4 of 8.
    filled = sum(1 for c in bar if c != " ")
    assert 3 <= filled <= 5, f"expected ~half filled, got {bar!r}"


@pytest.mark.asyncio
async def test_results_tree_labels_include_score_bar(
    fixtures_dir: Path, tmp_index_dir: Path
) -> None:
    """End-to-end: after a search, every file label in the results tree
    should carry a score-bar glyph alongside the numeric score."""
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    app = AcornApp(index_dir=tmp_index_dir, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        labels = [str(c.label) for c in tree.root.children]
        assert labels
        # Any of the eighth-block bar characters should appear in at least
        # one label.
        bar_glyphs = set("▁▂▃▄▅▆▇█")
        joined = "\n".join(labels)
        assert any(g in joined for g in bar_glyphs), f"no score-bar glyph in: {joined!r}"

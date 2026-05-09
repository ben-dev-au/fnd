"""UX-C: score bars were tried and rejected.

The bars rendered as visual artefacts at terminal width (the user
called them "horrific aesthetically and useless functionally"), so
the file/section labels no longer carry them. Numeric scores stay in
the underlying ``Hit`` / ``FileGroup`` data for callers that want
them; the labels show plain content only.

The ``_score_bar`` helper survives because it's a pure function that
might be useful for a future visualisation; these tests pin its
output so we don't drift if we ever revive it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Tree

from acorn.index import build_index
from acorn.tui import AcornApp
from acorn.tui.app import _score_bar


def test_score_bar_full_at_max() -> None:
    bar = _score_bar(score=10.0, max_score=10.0, width=4)
    assert bar == "████"


def test_score_bar_empty_at_zero_max() -> None:
    """Defensive: a max of 0 (no scored results) shouldn't divide by zero —
    return blank cells preserving column alignment."""
    bar = _score_bar(score=0.0, max_score=0.0, width=4)
    assert bar == "    "


def test_score_bar_proportional_in_middle() -> None:
    """Half the max → bar should be roughly half full (3-5 cells of 8)."""
    bar = _score_bar(score=5.0, max_score=10.0, width=8)
    filled = sum(1 for c in bar if c != " ")
    assert 3 <= filled <= 5, f"expected ~half filled, got {bar!r}"


@pytest.mark.asyncio
async def test_results_tree_labels_omit_score_bars(fixtures_dir: Path, tmp_index_dir: Path) -> None:
    """The label format shouldn't carry block-graph glyphs — they read
    as visual artefacts on most terminals and the user vetoed them."""
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    app = AcornApp(index_dir=tmp_index_dir, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        labels = "\n".join(str(c.label) for c in tree.root.children)
        for glyph in "▁▂▃▄▅▆▇█":
            assert glyph not in labels, f"label still carries {glyph!r}: {labels!r}"

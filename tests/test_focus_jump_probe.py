"""Probe: does the results-tree cursor jump back to the first file
after a preview load completes?

Reported behaviour: with two or more matching files in the tree, the
user clicks on a non-top file's section; the preview loads; once
loading completes, the cursor visibly jumps from the just-loaded file
back to the first file in the results tree.

This probe simulates the workflow headlessly and prints the cursor
trajectory so we can see exactly where the move happens (or doesn't).
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def multi_match_corpus(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    root.mkdir()
    # Several files all matching the same query so the results tree has
    # multiple parents to navigate between.
    for i in range(5):
        (root / f"doc_{i:02d}.md").write_text(
            "# Title\n\n" + ("apple " * 5 + "\n") * 6 + "\n## More\n\n" + ("apple " * 5 + "\n") * 6
        )
    return root


@pytest.mark.asyncio
async def test_cursor_does_not_jump_to_top_after_preview_load(
    multi_match_corpus: Path, tmp_index_dir: Path
) -> None:
    from textual.widgets import Tree

    from acorn.index import build_index
    from acorn.tui import AcornApp

    build_index(roots=[multi_match_corpus], index_dir=tmp_index_dir, collection="default")
    app = AcornApp(index_dir=tmp_index_dir, initial_query="apple")
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        for _ in range(20):
            await pilot.pause()
            if len(tree.root.children) > 1:
                break
        results = list(tree.root.children)
        assert len(results) >= 2, f"need at least 2 result files; got {len(results)}"
        # Realistic scenario: user has expanded file 2 (it was previously
        # opened/clicked on) and the cursor is on a section under it.
        target = results[1]
        target.expand()
        await pilot.pause()
        assert target.children, "second file should have section children"
        # Land on the first section of file 2.
        first_section = target.children[0]
        tree.cursor_line = first_section.line
        await pilot.pause()
        before = tree.cursor_line
        # Trigger preview load by selecting that section.
        tree.post_message(Tree.NodeSelected(first_section))
        # Let the preview load complete.
        cursor_trace: list[int] = [before]
        for _ in range(20):
            await pilot.pause()
            cursor_trace.append(tree.cursor_line)
        print(f"\nCursor trajectory: {cursor_trace}")
        print(f"Before NodeSelected: line {before}")
        print(f"After preview settled: line {tree.cursor_line}")
        # If the cursor jumped to the first file (line 0 or 1), this is
        # the reported bug.
        assert tree.cursor_line == before, (
            f"Cursor jumped from line {before} to line {tree.cursor_line} "
            f"after preview load — trajectory: {cursor_trace}"
        )

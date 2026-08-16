"""Regression: a stale ``NodeHighlighted`` must not load a row the cursor left.

``Tree.NodeHighlighted`` is a message, so it is handled a tick or more after the
cursor actually moved. Two searches in quick succession each rebuild the results
tree and each post highlight events; the earlier rebuild's events can still be
in the queue when the later one's have already been handled. ``_load_result_node``
took whatever node the event carried, so a stale echo scheduled a preview load
for a row the user had already left — and because it arrived LAST, last writer
won: the pane settled showing a file the cursor is not on, permanently.

Caught by ``dev/tools/preview_blank_fuzz.py`` (verdict ``wrong-file``), whose
trace shows the giveaway directly:

    SCHEDULE parent=a63e874a focus=14 cursor=f9b9da1b/6 @ _on_tree_highlight
    FIRE     target=(a63e874a, 14)    cursor=f9b9da1b/6
    RENDER   parent=a63e874a          cursor=f9b9da1b/6

A highlight event whose node is no longer the tree's cursor is stale by
definition — the cursor moving to a node is what posts the event in the first
place.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Tree

from fnd.index import build_index
from fnd.tui import FNDApp
from tests._pilot_wait import safe_pause, wait_until


@pytest.fixture
def built_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    root = tmp_path / "corpus"
    root.mkdir()
    for i in range(4):
        (root / f"note_{i:02d}.md").write_text(
            f"# Apples {i}\n\nThis note is about apples for query matching.\n\n"
            f"## More {i}\n\nAnother apple paragraph here.\n"
        )
    build_index(roots=[root], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_stale_highlight_echo_does_not_schedule_a_load(built_index: Path) -> None:
    app = FNDApp(index_dir=built_index, initial_query="apple")
    async with app.run_test() as pilot:
        await safe_pause(pilot)
        assert app._search.groups, "setup — query produced no results"
        tree = app.query_one("#results_pane", Tree)

        current = tree.cursor_node
        assert current is not None, "setup — the cursor should be parked on a row"
        assert isinstance(current.data, dict), "setup — the cursor row is a result row"
        stale = next(
            (
                n
                for n in tree.root.children
                if isinstance(n.data, dict) and n is not current and n.parent is not current
            ),
            None,
        )
        assert stale is not None, "fixture needs a second, different result row"

        scheduled: list[tuple[str, int]] = []
        app._preview.schedule_load = lambda parent_id, focus: scheduled.append(  # type: ignore[assignment,method-assign]
            (parent_id, focus)
        )

        # The echo the race delivers: a highlight for a row that is NOT the
        # cursor any more.
        app._on_tree_highlight(Tree.NodeHighlighted(stale))  # type: ignore[arg-type]
        assert not scheduled, (
            "a highlight event for a node the cursor has already left is stale — "
            "loading it makes the preview show a file the user is not on"
        )

        # The live one still loads, unchanged.
        app._on_tree_highlight(Tree.NodeHighlighted(current))  # type: ignore[arg-type]
        assert len(scheduled) == 1, "the cursor's own highlight must still load"


@pytest.mark.asyncio
async def test_enter_still_loads_the_selected_row(built_index: Path) -> None:
    """``NodeSelected`` (Enter) is an explicit user action on the row it names —
    it must keep loading even though it routes through the same helper."""
    app = FNDApp(index_dir=built_index, initial_query="apple")
    async with app.run_test() as pilot:
        await safe_pause(pilot)
        tree = app.query_one("#results_pane", Tree)
        nodes = [n for n in tree.root.children if isinstance(n.data, dict)]
        assert nodes, "fixture needs a result row"
        node = nodes[0]
        tree.cursor_line = node.line
        await safe_pause(pilot)

        scheduled: list[tuple[str, int]] = []
        app._preview.schedule_load = lambda parent_id, focus: scheduled.append(  # type: ignore[assignment,method-assign]
            (parent_id, focus)
        )
        app._on_results_selected(Tree.NodeSelected(node))  # type: ignore[arg-type]
        assert scheduled, "Enter on the highlighted row must load it"


@pytest.mark.asyncio
async def test_two_rapid_queries_leave_the_preview_on_the_cursor_row(built_index: Path) -> None:
    """End-to-end shape of the bug: back-to-back searches interleave their tree
    rebuilds' highlight events. Whatever lands last, the preview must agree with
    the cursor."""
    app = FNDApp(index_dir=built_index, initial_query="apple")
    async with app.run_test() as pilot:
        await safe_pause(pilot)
        # Both issued WITHOUT waiting in between: the interleaving of the two
        # tree rebuilds' highlight events is the whole point of this test, and
        # gating the first one on idle serialises them and tests nothing.
        app._search.run("apples")
        app._search.run("apple")
        await wait_until(pilot, lambda: app._search.idle, message="searches never settled")
        for _ in range(12):
            await safe_pause(pilot)

        preview = app._preview
        target = preview.cursor_target()
        if target is None:
            pytest.skip("no result selected after the rapid queries")
        assert preview.showing_parent() in (None, target[0]), (
            "the preview must never settle on a file other than the cursor's"
        )

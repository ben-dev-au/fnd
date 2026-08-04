"""A broken highlighter must never subtract results.

The engine's match is what makes a result a result. If paintability were allowed
to gate the results pane, a bug in the highlighter would remove rows silently —
the user would never learn they had missed a match, and the bug would never be
discovered. So an unpaintable match is *marked*, never withheld.

This test simulates the worst case: a spec that matches nothing anywhere, i.e. a
highlighter that has stopped working entirely.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Tree

from fnd.config import load
from fnd.index import build_index
from fnd.matching import MatchSpec
from fnd.tui import FNDApp
from fnd.tui.results_labels import _UNLOCATABLE_GLYPH

# Non-empty (so the evidence check engages) but present in no document — the
# shape a broken highlighter would produce. An *empty* spec would short-circuit
# to "nothing to locate" and prove nothing.
BLIND_SPEC = MatchSpec.from_query("zzqqxx")


@pytest.fixture
def app_with_results(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> FNDApp:
    docs = tmp_path / "docs"
    docs.mkdir()
    for i in range(6):
        (docs / f"file{i:02d}.md").write_text(
            f"# Heading {i:02d}\n\nglimmer content number {i} lorem ipsum dolor.\n",
            encoding="utf-8",
        )
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.notes.sources]]
            path = "{docs.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    build_index(roots=[docs], index_dir=tmp_index_dir, collection="notes")
    return FNDApp(
        config=load(cfg_path),
        index_dir=tmp_index_dir,
        collection="notes",
        initial_query="glimmer",
    )


def _rows(tree: Tree[object]) -> list[tuple[str, int]]:
    """``(parent_id, chunk_seq)`` for every section row, in order."""
    out: list[tuple[str, int]] = []
    for file_node in tree.root.children:
        for leaf in file_node.children:
            data = leaf.data
            assert isinstance(data, dict)
            hit = data["hit"]
            out.append((hit.parent_id, hit.chunk_seq))
    return out


def _blind(app: FNDApp, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the app's live spec with one that matches nothing — a
    highlighter that has stopped working. monkeypatch restores the real
    property afterwards."""
    monkeypatch.setattr(type(app), "_effective_match_spec", property(lambda _self: BLIND_SPEC))


@pytest.mark.asyncio
async def test_blind_highlighter_removes_no_results(
    app_with_results: FNDApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with app_with_results.run_test() as pilot:
        for _ in range(8):
            await pilot.pause()
        tree = app_with_results.query_one("#results_pane", Tree)
        healthy_rows = _rows(tree)
        healthy_files = len(tree.root.children)
        assert healthy_rows, "fixture produced no results to compare against"

        _blind(app_with_results, monkeypatch)
        app_with_results._results.refresh()
        await pilot.pause()

        blind_tree = app_with_results.query_one("#results_pane", Tree)
        assert _rows(blind_tree) == healthy_rows
        assert len(blind_tree.root.children) == healthy_files


@pytest.mark.asyncio
async def test_blind_highlighter_marks_every_row(
    app_with_results: FNDApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not merely retained — visibly flagged, so the regression is discoverable
    rather than a silent oddity."""
    async with app_with_results.run_test() as pilot:
        for _ in range(8):
            await pilot.pause()
        tree = app_with_results.query_one("#results_pane", Tree)
        healthy = [str(leaf.label) for f in tree.root.children for leaf in f.children]
        assert healthy, "fixture produced no rows to compare against"
        assert not any(_UNLOCATABLE_GLYPH in label for label in healthy)

        _blind(app_with_results, monkeypatch)
        app_with_results._results.refresh()
        await pilot.pause()

        tree = app_with_results.query_one("#results_pane", Tree)
        marked = [str(leaf.label) for f in tree.root.children for leaf in f.children]
        assert marked
        assert all(_UNLOCATABLE_GLYPH in label for label in marked)

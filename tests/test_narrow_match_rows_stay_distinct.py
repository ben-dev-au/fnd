"""At 62 columns 24 sibling match rows all painted `section`, one string.

File rows already elide through `label_budget` and stay identifiable
(`bi…00.md`); match rows ignored the budget, so the pane hard-clipped them at
the border and the discriminating character was the one that fell off. Match
rows are most of the tree, and choosing between them is the pane's job.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Tree

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp


@pytest.fixture
def indexed(tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    root = tmp_path / "notes"
    root.mkdir()
    body = "\n\n".join(f"## section {j}\n\nThe quicksilver fox {j} lives here.\n" for j in range(8))
    (root / "big.md").write_text(f"# Document\n\n{body}\n", encoding="utf-8")
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
    return load(cfg_path)


async def _match_rows(cfg: Config, tmp_index_dir: Path, width: int) -> list[str]:
    app = FNDApp(
        index_dir=tmp_index_dir, config=cfg, collection="notes", initial_query="quicksilver"
    )
    async with app.run_test(size=(width, 24)) as pilot:
        for _ in range(30):
            await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        for node in list(tree.root.children):
            node.expand()
        for _ in range(10):
            await pilot.pause()
        rows = ["".join(s.text for s in strip) for strip in app.screen._compositor.render_strips()]
    return [r for r in rows if "├" in r or "└" in r]


@pytest.mark.asyncio
async def test_sibling_match_rows_do_not_paint_identically(
    indexed: Config, tmp_index_dir: Path
) -> None:
    """A row you cannot tell from its neighbour cannot be chosen between."""
    rows = await _match_rows(indexed, tmp_index_dir, 62)

    assert len(rows) >= 4, rows
    assert len(set(rows)) == len(rows), rows


@pytest.mark.asyncio
async def test_a_wide_pane_still_shows_the_snippet(indexed: Config, tmp_index_dir: Path) -> None:
    """The control: the budget must not strip context a wide pane can hold."""
    rows = await _match_rows(indexed, tmp_index_dir, 120)

    assert any("quicksilver" in r for r in rows), rows

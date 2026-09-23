"""At 62 columns the Filters rows were byte-identical with a filter set and
with none.

The pane exists to show filter state. Out of width it kept every label and
dropped every value, so `Tags (none indexed)`, a filter that can do nothing,
painted the same as three live ones.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Tree

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.scope_panel import _branch_row


@pytest.fixture
def indexed(tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "a.md").write_text("# A\n\nquicksilver here.\n", encoding="utf-8")
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


async def _branch_rows(cfg: Config, tmp_index_dir: Path, *, kinds: list[str]) -> list[str]:
    app = FNDApp(
        index_dir=tmp_index_dir, config=cfg, collection="notes", initial_query="quicksilver"
    )
    async with app.run_test(size=(62, 30)) as pilot:
        for _ in range(30):
            await pilot.pause()
        app._scope.filter_kinds = kinds
        app._scope.refresh_filters_panel()
        for _ in range(10):
            await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        rows = ["".join(s.text for s in strip) for strip in app.screen._compositor.render_strips()]
        _ = tree
    return [r for r in rows if "▶" in r]


@pytest.mark.asyncio
async def test_a_set_filter_looks_different_from_no_filter(
    indexed: Config, tmp_index_dir: Path
) -> None:
    """The mechanical proof: same width, same rows, one filter apart."""
    off = await _branch_rows(indexed, tmp_index_dir, kinds=[])
    on = await _branch_rows(indexed, tmp_index_dir, kinds=["md"])

    assert off, off
    assert on, on
    assert off != on, off


def test_a_row_gives_up_its_label_before_its_value() -> None:
    """The ladder: pad, tighten, compact the value, then eat the label.

    The label always survives: it is what the row is found by. When neither
    half fits whole the value is elided with a marker, never dropped.
    """
    assert _branch_row("File type", "1 of 2", "1/2", 0) == "File type        (1 of 2)"
    assert _branch_row("File type", "1 of 2", "1/2", 25) == "File type        (1 of 2)"
    assert _branch_row("File type", "1 of 2", "1/2", 18) == "File type (1 of 2)"
    assert _branch_row("File type", "1 of 2", "1/2", 16) == "File type (1/2)"
    assert _branch_row("File type", "1 of 2", "1/2", 10) == "Fil… (1/2)"
    assert _branch_row("Tags", "none indexed", "0 tags", 16) == "Tags (0 tags)"
    assert _branch_row("Tags", "none indexed", "0 tags", 12) == "Ta… (0 tags)"
    assert _branch_row("Tags", "none indexed", "0 tags", 6) == "Tags (0 tags)"
    assert (
        _branch_row("Tags", "1 still filtering, no rows to show", "1 filtering, no rows", 22)
        == "Tags (1 filtering, n…)"
    )


def test_a_name_is_elided_rather_than_silently_shortened() -> None:
    """`research-notes` painted as `research-note` in the sidebar: a
    collection that does not exist, and indistinguishable from one that could.
    A label that is user data always carries the marker."""
    assert _branch_row("research-notes", "2 sources", "2 src", 17, column=0) == "research… (2 src)"
    assert _branch_row("research-notes", "2 sources", "2 src", 5, column=0) == "r… (2 src)"


def test_a_fixed_label_stays_whole_at_the_last_rung() -> None:
    """The control: `Tags` is four known characters, so it clips rather than
    turning into `T…` and costing a cell to say nothing."""
    assert _branch_row("Tags", "none indexed", "0 tags", 6) == "Tags (0 tags)"

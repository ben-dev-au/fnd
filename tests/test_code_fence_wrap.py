"""Fenced code blocks wrap to the pane instead of scrolling horizontally.

Textual's stock MarkdownFence scrolls long lines sideways; the app CSS
hides overflow-x and pins the inner label to the fence width so code
reflows. Pins: no horizontal scroll, and no horizontal clip of content.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Label, Tree
from textual.widgets._markdown import MarkdownFence

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.notes.sources]]
            path = "/tmp/notes"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.fixture
def fence_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    notes = tmp_path / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    long_line = "x = " + " + ".join(f"variable_number_{i}" for i in range(40))
    body = f"# Doc\n\nThe glimmer marker.\n\n```python\n{long_line}\nshort = 1\n```\n"
    (notes / "Doc.md").write_text(body, encoding="utf-8")
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_code_fence_wraps_without_horizontal_scroll(cfg: Config, fence_index: Path) -> None:
    app = FNDApp(index_dir=fence_index, config=cfg, collection="notes", initial_query="glimmer")
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        tree.focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause()

        fences = list(app.query(MarkdownFence))
        assert fences, "expected a fenced code block to mount"
        fence = fences[0]
        # Long line is far wider than the 70-col window; wrapping means the
        # fence has no horizontal scroll extent.
        assert fence.max_scroll_x == 0, fence.virtual_size
        # And the wrapped content isn't clipped on the right.
        label = fence.query_one("#code-content", Label)
        assert label.virtual_size.width <= label.size.width, (
            label.virtual_size,
            label.size,
        )
        # Wrapping the 831-char line produces many rows (it didn't stay on one).
        assert fence.size.height > 3, fence.size

"""The preview footer has advertised `j/k Scroll` and neither key did anything.

Ten presses moved nothing; `↓` worked. Textual's scroll view binds the arrows
only, and the rest of the app speaks vi keys (the settings list binds `up,k`
and `down,j`), so what was missing was the binding, not the claim.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.preview_scrollbar import MatchAwareScroll


@pytest.fixture
def long_note(tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    root = tmp_path / "notes"
    root.mkdir()
    body = "\n\n".join(f"## Section {i}\n\nsaffron paragraph {i}." for i in range(60))
    (root / "long.md").write_text(f"# Long\n\n{body}\n", encoding="utf-8")
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


async def _scrolled_by(app: FNDApp, key: str) -> float:
    async with app.run_test(size=(100, 30)) as pilot:
        for _ in range(30):
            await pilot.pause()
        pane = app.query_one("#preview_pane", MatchAwareScroll)
        pane.focus()
        await pilot.pause()
        before = pane.scroll_target_y
        for _ in range(4):
            await pilot.press(key)
            await pilot.pause()
        return pane.scroll_target_y - before


@pytest.mark.asyncio
async def test_j_scrolls_down(long_note: Config, tmp_index_dir: Path) -> None:
    app = FNDApp(
        index_dir=tmp_index_dir, config=long_note, collection="notes", initial_query="saffron"
    )

    moved = await _scrolled_by(app, "j")

    assert moved > 0, "j is advertised in the preview footer and did nothing"


@pytest.mark.asyncio
async def test_the_arrow_still_scrolls(long_note: Config, tmp_index_dir: Path) -> None:
    """The control: the key that already worked must keep working."""
    app = FNDApp(
        index_dir=tmp_index_dir, config=long_note, collection="notes", initial_query="saffron"
    )

    moved = await _scrolled_by(app, "down")

    assert moved > 0


@pytest.mark.asyncio
async def test_k_scrolls_back(long_note: Config, tmp_index_dir: Path) -> None:
    app = FNDApp(
        index_dir=tmp_index_dir, config=long_note, collection="notes", initial_query="saffron"
    )
    async with app.run_test(size=(100, 30)) as pilot:
        for _ in range(30):
            await pilot.pause()
        pane = app.query_one("#preview_pane", MatchAwareScroll)
        pane.focus()
        await pilot.pause()
        for _ in range(6):
            await pilot.press("j")
            await pilot.pause()
        mid = pane.scroll_target_y
        for _ in range(3):
            await pilot.press("k")
            await pilot.pause()
        back = pane.scroll_target_y

    assert mid > 0, "nothing to scroll back from"
    assert back < mid, "k did not scroll up"

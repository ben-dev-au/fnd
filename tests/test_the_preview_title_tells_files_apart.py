"""Both panes said `minutes.md` for two different files.

The results rows learned to share as much path as they need; the preview title
above them kept the bare basename, so opening one and then the other changed
nothing on screen.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.widgets.results_tree import ResultsTree


@pytest.fixture
def twins(tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    root = tmp_path / "notes"
    for folder in ("jan", "feb"):
        (root / folder).mkdir(parents=True)
        (root / folder / "minutes.md").write_text(
            f"# {folder}\n\nsaffron was discussed.\n", encoding="utf-8"
        )
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


@pytest.mark.asyncio
async def test_the_two_titles_differ(twins: Config, tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir, config=twins, collection="notes", initial_query="saffron")
    seen: list[str] = []
    async with app.run_test(size=(120, 30)) as pilot:
        for _ in range(30):
            await pilot.pause()
        tree = app.query_one("#results_pane", ResultsTree)
        tree.focus()
        await pilot.pause()
        for node in list(tree.root.children):
            tree.move_cursor(node)
            for _ in range(10):
                await pilot.pause()
            seen.append(app._preview_title())

    assert len(seen) == 2, seen
    assert seen[0] != seen[1], f"both files gave the same title: {seen}"
    assert all("minutes.md" in t for t in seen), seen


@pytest.mark.asyncio
async def test_a_unique_name_stays_bare(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: one file of that name keeps the short title."""
    root = tmp_path / "notes"
    root.mkdir()
    (root / "alone.md").write_text("saffron\n", encoding="utf-8")
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
    app = FNDApp(
        index_dir=tmp_index_dir, config=load(cfg_path), collection="notes", initial_query="saffron"
    )
    async with app.run_test(size=(120, 30)) as pilot:
        for _ in range(30):
            await pilot.pause()
        title = app._preview_title()

    assert title == "Preview: alone.md", title

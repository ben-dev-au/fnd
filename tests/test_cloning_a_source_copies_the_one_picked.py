"""Cloning a source copies the source picked, as the file holds it now.

`CloneSourcePickSourceScreen` listed a collection's sources from `app._config`,
a model loaded at launch, and passed the picked row's index to `clone_source`,
which reads the file. Once the file had moved on, the index named a different
source there, and that one was cloned instead.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest
from textual.screen import Screen
from textual.widgets import OptionList

from fnd.config import load
from fnd.tui import FNDApp
from fnd.tui.indexer_service import IndexerService
from fnd.tui.settings_screen import CloneSourcePickSourceScreen


@pytest.fixture
def cfg_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("one", "two", "three"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "a.md").write_text("saffron\n", encoding="utf-8")
    path = tmp_path / "config.toml"
    monkeypatch.setattr("fnd.config.default_config_path", lambda: path)
    monkeypatch.setattr(IndexerService, "reindex_with_warning", lambda self, name, **kw: None)
    _write(path, tmp_path, ("notes", "one"), ("papers", "two"), ("papers", "three"))
    return path


def _write(cfg_path: Path, root: Path, *rows: tuple[str, str]) -> None:
    cfg_path.write_text(
        "".join(
            textwrap.dedent(f"""
                [[collections.{collection}.sources]]
                path = "{(root / name).as_posix()}"
            """)
            for collection, name in rows
        ),
        encoding="utf-8",
    )


def _notes(cfg_path: Path) -> list[str]:
    return [Path(s.path).name for s in load(cfg_path).collections["notes"].sources]


async def _pick_first(app: FNDApp, pilot: Any, cfg_path: Path, *after: tuple[str, str]) -> None:
    app._config = load()
    app.push_screen(Screen())
    app.push_screen(Screen())
    app.push_screen(
        CloneSourcePickSourceScreen(source_collection="papers", target_collection="notes")
    )
    for _ in range(5):
        await pilot.pause()
    _write(cfg_path, cfg_path.parent, *after)
    options = app.screen.query_one("#clone_list", OptionList)
    options.highlighted = 0
    options.action_select()
    for _ in range(10):
        await pilot.pause()


@pytest.mark.asyncio
async def test_a_reordered_file_still_clones_the_source_picked(
    cfg_path: Path, tmp_index_dir: Path
) -> None:
    """The copy is of the row the user picked, wherever the file now puts it."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        await _pick_first(
            app, pilot, cfg_path, ("notes", "one"), ("papers", "three"), ("papers", "two")
        )

    assert _notes(cfg_path) == ["one", "two"]


@pytest.mark.asyncio
async def test_a_source_gone_from_the_file_is_not_cloned(
    cfg_path: Path, tmp_index_dir: Path
) -> None:
    """A picked source the file no longer holds is refused, and nothing is copied."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        await _pick_first(app, pilot, cfg_path, ("notes", "one"), ("papers", "three"))

    assert _notes(cfg_path) == ["one"]

"""Renaming a collection writes the collection as the file holds it now.

`RenameCollectionScreen` copied the collection out of `app._config`, a model
loaded at launch, and wrote it under the new name: a source added from a shell
since launch was dropped by the rename, a collection deleted by hand came back
under the new name, and one created by hand under the new name was overwritten.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest
from textual.screen import Screen
from textual.widgets import Input

from fnd.config import load
from fnd.tui import FNDApp
from fnd.tui.indexer_service import IndexerService
from fnd.tui.settings_screen import RenameCollectionScreen


@pytest.fixture
def cfg_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("one", "two", "three"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "a.md").write_text("saffron\n", encoding="utf-8")
    path = tmp_path / "config.toml"
    monkeypatch.setattr("fnd.config.default_config_path", lambda: path)
    monkeypatch.setattr(IndexerService, "reindex_with_warning", lambda self, name, **kw: None)
    _write(path, tmp_path, ("notes", "one"), ("notes", "two"))
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


def _layout(cfg_path: Path) -> dict[str, list[str]]:
    cfg = load(cfg_path)
    return {c: [Path(s.path).name for s in col.sources] for c, col in cfg.collections.items()}


async def _rename(app: FNDApp, pilot: Any, then: Any, new_name: str) -> None:
    app._config = load()
    app.push_screen(Screen())
    app.push_screen(RenameCollectionScreen(collection_name="notes"))
    for _ in range(5):
        await pilot.pause()
    then()
    app.screen.query_one("#new_collection_name", Input).value = new_name
    await pilot.press("enter")
    for _ in range(10):
        await pilot.pause()


@pytest.mark.asyncio
async def test_a_source_added_from_a_shell_survives_a_rename(
    cfg_path: Path, tmp_path: Path, tmp_index_dir: Path
) -> None:
    """A source added to the collection after launch moves with it to the new name."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        await _rename(
            app,
            pilot,
            lambda: _write(
                cfg_path, tmp_path, ("notes", "one"), ("notes", "two"), ("notes", "three")
            ),
            "archive",
        )

    assert _layout(cfg_path) == {"archive": ["one", "two", "three"]}


@pytest.mark.asyncio
async def test_a_collection_made_since_launch_is_not_overwritten(
    cfg_path: Path, tmp_path: Path, tmp_index_dir: Path
) -> None:
    """A rename onto a name the file now holds is refused and writes nothing."""
    added = (("notes", "one"), ("notes", "two"), ("archive", "three"))
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        await _rename(app, pilot, lambda: _write(cfg_path, tmp_path, *added), "archive")

    assert _layout(cfg_path) == {"notes": ["one", "two"], "archive": ["three"]}


@pytest.mark.asyncio
async def test_a_collection_removed_since_launch_is_not_brought_back(
    cfg_path: Path, tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Renaming a collection the file no longer holds is refused and writes nothing."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        await _rename(
            app, pilot, lambda: _write(cfg_path, tmp_path, ("papers", "three")), "archive"
        )

    assert _layout(cfg_path) == {"papers": ["three"]}

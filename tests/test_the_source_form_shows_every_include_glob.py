"""The source form's include row holds every include glob, and saves what it holds.

``**/*.txt`` beside ``notes/**`` is an include glob like its neighbour: the
model folds type globs into file types only when nothing else is listed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fnd.config import CollectionConfig, SourceConfig, load, write_collection
from fnd.tui import FNDApp
from fnd.tui.settings_screen import SettingsList, SourceFormScreen
from fnd.walk import walk_sources
from tests._pilot_wait import screen_ready, wait_until

_MIXED = ["**/*.txt", "notes/**"]


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    root = tmp_path / "vault"
    (root / "notes").mkdir(parents=True)
    for rel in ("a.md", "b.txt", "notes/c.md"):
        (root / rel).write_text("hello\n", encoding="utf-8")
    write_collection(
        config_path=cfg_path,
        name="probe",
        collection=CollectionConfig(sources=[SourceConfig(path=root, includes=_MIXED)]),
    )
    return root


def _indexed(root: Path) -> set[str]:
    source = load().collections["probe"].sources[0]
    return {p.relative_to(root.resolve()).as_posix() for p in walk_sources(sources=[source])}


def _painted(screen: Any, label: str) -> str:
    rows = ["".join(s.text for s in strip) for strip in screen._compositor.render_strips()]
    return next(r for r in rows if label in r)


async def _open_form(app: FNDApp, pilot: Any) -> SourceFormScreen:
    app._config = load()
    app.push_screen(SourceFormScreen(collection_name="probe", source_index=0))
    screen = await screen_ready(pilot, app, SourceFormScreen)
    assert isinstance(screen, SourceFormScreen)
    return screen


async def _save(app: FNDApp, pilot: Any) -> None:
    await pilot.press("ctrl+s")
    await wait_until(pilot, lambda: not isinstance(app.screen, SourceFormScreen))


@pytest.mark.asyncio
async def test_the_row_shows_a_type_glob_beside_a_path_glob(vault: Path, tmp_path: Path) -> None:
    """Every include glob is painted on the row that edits them."""
    app = FNDApp(index_dir=tmp_path / "idx")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        form = await _open_form(app, pilot)
        assert "**/*.txt, notes/**" in _painted(form, "Restrict to these paths")


@pytest.mark.asyncio
async def test_emptying_the_row_indexes_the_whole_folder(vault: Path, tmp_path: Path) -> None:
    """Clearing "Restrict to these paths" leaves no include glob and no file-type rule."""
    assert _indexed(vault) == {"b.txt", "notes/c.md"}, "the premise"
    app = FNDApp(index_dir=tmp_path / "idx")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        form = await _open_form(app, pilot)
        rows = form.query_one(SettingsList)
        rows.cursor_index = next(
            i for i, it in enumerate(rows._items) if it.id == "form.includes_custom"
        )
        await pilot.press("enter")
        await pilot.press("ctrl+u", "enter")
        await wait_until(pilot, lambda: form._fields["includes_custom"] == "")
        await _save(app, pilot)

    source = load().collections["probe"].sources[0]
    assert source.includes == []
    assert source.filters is None or source.filters.kinds is None
    assert _indexed(vault) == {"a.md", "b.txt", "notes/c.md"}


@pytest.mark.asyncio
async def test_an_unedited_save_keeps_the_mixed_list(vault: Path, tmp_path: Path) -> None:
    """Opening the form and saving unchanged writes the include globs back as they were."""
    app = FNDApp(index_dir=tmp_path / "idx")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _open_form(app, pilot)
        await _save(app, pilot)

    assert load().collections["probe"].sources[0].includes == _MIXED

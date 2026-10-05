"""Saving writes config and nothing else; the index is shown out of date instead (D3)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.widgets import OptionList

from fnd.config import CollectionConfig, SourceConfig, load, write_collection
from fnd.index_freshness import Ledger, indexed_with
from fnd.tui import FNDApp
from fnd.tui.settings_screen import SourceFormScreen


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg_path = tmp_path / "config.toml"
    (tmp_path / "notes").mkdir()
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    write_collection(
        config_path=cfg_path,
        name="notes",
        collection=CollectionConfig(sources=[SourceConfig(path=tmp_path / "notes")]),
    )
    return cfg_path


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(
        "fnd.tui.indexer_service.IndexerService.reindex_with_warning",
        lambda _self, name, **_kw: calls.append(name),
    )
    return calls


@pytest.fixture
def index_dir(tmp_path: Path, config_file: Path) -> Path:
    """An index recorded as current for the config as written."""
    path = tmp_path / "idx"
    path.mkdir()
    cfg = load(config_file)
    Ledger(path).record("notes", indexed_with(cfg.collections["notes"], cfg.defaults))
    return path


def _notices(app: FNDApp, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    real = app.notify

    def _spy(message: str, *args: Any, **kwargs: Any) -> None:
        seen.append(str(message))
        real(message, *args, **kwargs)

    monkeypatch.setattr(app, "notify", _spy)
    return seen


async def _settle(pilot: Any, n: int = 20) -> None:
    for _ in range(n):
        await pilot.pause()


async def _open_form(app: FNDApp, pilot: Any, index: int | None = 0) -> SourceFormScreen:
    app.push_screen(SourceFormScreen(collection_name="notes", source_index=index))
    await _settle(pilot)
    form = app.screen
    assert isinstance(form, SourceFormScreen)
    return form


@pytest.mark.asyncio
async def test_saving_a_source_starts_no_run(
    config_file: Path, index_dir: Path, started: list[str]
) -> None:
    """^s on a changed source writes the file and indexes nothing."""
    app = FNDApp(index_dir=index_dir, config=load(config_file))
    async with app.run_test(size=(110, 30)) as pilot:
        await _settle(pilot, 5)
        form = await _open_form(app, pilot)
        form._fields["excludes_custom"] = "build/**"
        await pilot.press("ctrl+s")
        await _settle(pilot, 8)
        left = not isinstance(app.screen, SourceFormScreen)
    assert started == []
    assert left
    assert "build/**" in config_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_the_save_toast_names_the_update_and_the_key(
    config_file: Path, index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user learns what is out of date, and how to run it, as they save."""
    app = FNDApp(index_dir=index_dir, config=load(config_file))
    async with app.run_test(size=(110, 30)) as pilot:
        await _settle(pilot, 5)
        seen = _notices(app, monkeypatch)
        form = await _open_form(app, pilot)
        form._fields["excludes_custom"] = "build/**"
        await pilot.press("ctrl+s")
        await _settle(pilot, 8)
    assert any("is outdated" in m and "press u" in m for m in seen), seen


@pytest.mark.asyncio
async def test_an_untouched_new_source_leaves_without_asking(
    config_file: Path, index_dir: Path
) -> None:
    """Opening Add source and pressing Esc has nothing to lose."""
    app = FNDApp(index_dir=index_dir, config=load(config_file))
    async with app.run_test(size=(110, 30)) as pilot:
        await _settle(pilot, 5)
        await _open_form(app, pilot, index=None)
        await pilot.press("escape")
        await _settle(pilot, 5)
        left = not isinstance(app.screen, SourceFormScreen)
        names = [type(s).__name__ for s in app.screen_stack]
    assert left, names
    assert "UnsavedChangesScreen" not in names


@pytest.mark.asyncio
async def test_deleting_a_source_starts_no_run(
    config_file: Path, index_dir: Path, started: list[str], tmp_path: Path
) -> None:
    """Removing a source writes the config; the collection is then marked out of date."""
    second = tmp_path / "more"
    second.mkdir()
    cfg = load(config_file)
    cfg.collections["notes"].sources.append(SourceConfig(path=second))
    write_collection(config_path=config_file, name="notes", collection=cfg.collections["notes"])
    app = FNDApp(index_dir=index_dir, config=load(config_file))
    async with app.run_test(size=(110, 30)) as pilot:
        await _settle(pilot, 5)
        await _open_form(app, pilot, index=1)
        await pilot.press("ctrl+d")
        await _settle(pilot, 8)
        options = app.screen.query_one("#confirm_list", OptionList)
        options.highlighted = next(i for i, o in enumerate(options._options) if o.id == "yes")
        options.action_select()
        await _settle(pilot, 8)
    assert started == []
    assert len(load(config_file).collections["notes"].sources) == 1


@pytest.mark.asyncio
async def test_deleting_over_unsaved_edits_says_they_go_too(
    config_file: Path, index_dir: Path, tmp_path: Path
) -> None:
    """Delete closes the form, so its unsaved edits go with it, and the dialog says so."""
    app = FNDApp(index_dir=index_dir, config=load(config_file))
    async with app.run_test(size=(110, 30)) as pilot:
        await _settle(pilot, 5)
        form = await _open_form(app, pilot)
        form._fields["excludes_custom"] = "build/**"
        await pilot.press("ctrl+d")
        await _settle(pilot, 8)
        painted = "\n".join(
            "".join(seg.text for seg in strip) for strip in app.screen._compositor.render_strips()
        )
    assert "Unsaved edits to this source are discarded too" in painted


@pytest.mark.asyncio
async def test_adding_a_collection_starts_no_run_and_opens_its_page(
    config_file: Path, index_dir: Path, started: list[str], tmp_path: Path
) -> None:
    """The wizard writes the collection and lands on its page, Update index marked needed."""
    from fnd.tui.menu import _summary_collection_update
    from fnd.tui.settings_screen import AddCollectionWizard, SettingsScreen

    research = tmp_path / "research"
    research.mkdir()
    app = FNDApp(index_dir=index_dir, config=load(config_file))
    async with app.run_test(size=(110, 30)) as pilot:
        await _settle(pilot, 5)
        app.push_screen(AddCollectionWizard())
        await _settle(pilot)
        wizard = app.screen
        assert isinstance(wizard, AddCollectionWizard)
        wizard._fields["name"] = "research"
        wizard._fields["path"] = str(research)
        await pilot.press("ctrl+s")
        await _settle(pilot, 10)
        page = app.screen
        on_page = isinstance(page, SettingsScreen) and page._breadcrumb == (
            "Collections",
            "research",
        )
        needed = _summary_collection_update(app, "research")
    assert "research" in load(config_file).collections
    assert started == []
    assert on_page
    assert needed.startswith("Not indexed"), needed


async def _rename(app: FNDApp, pilot: Any, new_name: str) -> Any:
    from textual.screen import Screen
    from textual.widgets import Input

    from fnd.tui.settings_screen import RenameCollectionScreen

    app.push_screen(Screen())  # the collection page the rename pops past
    await _settle(pilot, 4)
    screen = RenameCollectionScreen(collection_name="notes")
    app.push_screen(screen)
    await _settle(pilot, 8)
    screen.query_one("#new_collection_name", Input).value = new_name
    await pilot.press("enter")
    await _settle(pilot, 10)
    return screen


@pytest.mark.asyncio
async def test_an_invalid_name_is_refused_not_a_crash(config_file: Path, index_dir: Path) -> None:
    """A name the config cannot hold stays on screen with the rule it broke."""
    from fnd.tui.settings_screen import RenameCollectionScreen

    app = FNDApp(index_dir=index_dir, config=load(config_file))
    async with app.run_test(size=(110, 30)) as pilot:
        await _settle(pilot, 5)
        await _rename(app, pilot, "probe/b")
        still = isinstance(app.screen, RenameCollectionScreen)
        running = app.is_running
    assert still
    assert running
    assert "notes" in load(config_file).collections


@pytest.mark.asyncio
async def test_a_rename_drops_the_old_name_and_starts_no_run(
    config_file: Path,
    index_dir: Path,
    started: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rename is a setting: written at once, old documents dropped, the new name not indexed yet."""
    dropped: list[str] = []
    monkeypatch.setattr("fnd.index.drop_collection", lambda _dir, name, **_kw: dropped.append(name))
    app = FNDApp(index_dir=index_dir, config=load(config_file))
    async with app.run_test(size=(110, 30)) as pilot:
        await _settle(pilot, 5)
        seen = _notices(app, monkeypatch)
        await _rename(app, pilot, "research")
        await app.workers.wait_for_complete()
        await _settle(pilot, 6)
    assert set(load(config_file).collections) == {"research"}
    assert dropped == ["notes"]
    assert started == []
    assert Ledger(index_dir).recorded("notes") is None
    assert any("not indexed yet" in m and "press u" in m for m in seen), seen

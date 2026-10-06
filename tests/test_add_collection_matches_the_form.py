"""Add collection: the collection's name, then its first source exactly as the source form shows it."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import TextArea

from fnd.config import EXCLUDES_PRESETS, load, write_collection
from fnd.tui import FNDApp
from fnd.tui.menu import KIND_HEADER
from fnd.tui.settings_screen import AddCollectionWizard, SettingsList

NODE = EXCLUDES_PRESETS["node_modules"]["globs"]


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from fnd.config import CollectionConfig, SourceConfig

    cfg_path = tmp_path / "config.toml"
    (tmp_path / "notes").mkdir()
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    write_collection(
        config_path=cfg_path,
        name="notes",
        collection=CollectionConfig(sources=[SourceConfig(path=tmp_path / "notes")]),
    )
    return cfg_path


async def _settle(pilot: Any, n: int = 15) -> None:
    for _ in range(n):
        await pilot.pause()


async def _wizard(app: FNDApp, pilot: Any) -> AddCollectionWizard:
    app.push_screen(AddCollectionWizard())
    await _settle(pilot)
    wizard = app.screen
    assert isinstance(wizard, AddCollectionWizard)
    return wizard


def _rows(wizard: AddCollectionWizard) -> list[tuple[str | None, str]]:
    return [
        (i.subsection, f"[{i.label}]" if i.kind == KIND_HEADER else i.id)
        for i in wizard.query_one(SettingsList)._items
    ]


@pytest.mark.asyncio
async def test_what_gets_indexed_sits_inside_the_source(
    config_file: Path, tmp_index_dir: Path
) -> None:
    """A heading beside Source would read as the collection's; the Source box holds them."""
    app = FNDApp(index_dir=tmp_index_dir, config=load(config_file))
    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot, 5)
        rows = _rows(await _wizard(app, pilot))
    assert rows == [
        (None, "[Collection]"),
        (None, "wiz.name"),
        ("Source", "wiz.path"),
        ("Source", "wiz.follow_symlinks"),
        ("Source", "[What gets indexed]"),
        ("Source", "wiz.filters"),
        ("Source", "wiz.frontmatter"),
        ("Source", "wiz.includes_custom"),
    ]


@pytest.mark.asyncio
async def test_the_frontmatter_row_and_index_filters_edit_one_rule(
    config_file: Path, tmp_index_dir: Path
) -> None:
    """One value, one editor: a rule typed on the row is the Index filters rule."""
    from fnd.tui.settings_screen import FilterBrowserScreen, RuleTextScreen

    app = FNDApp(index_dir=tmp_index_dir, config=load(config_file))
    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot, 5)
        wizard = await _wizard(app, pilot)
        wizard._edit_frontmatter()
        await _settle(pilot)
        editor = app.screen
        assert isinstance(editor, RuleTextScreen)
        editor.query_one("#rule_text", TextArea).text = "status == 'done'"
        await _settle(pilot, 4)
        await pilot.press("escape")
        await _settle(pilot)
        wizard._open_filters()
        await _settle(pilot)
        browser = app.screen
        assert isinstance(browser, FilterBrowserScreen)
        shown = browser._spec.frontmatter
    assert shown == "status == 'done'"


@pytest.mark.asyncio
async def test_index_filters_before_a_path_save_with_the_collection(
    config_file: Path, tmp_index_dir: Path, tmp_path: Path
) -> None:
    """No path yet means no sample, not a crash; the filters still save."""
    research = tmp_path / "research"
    research.mkdir()
    app = FNDApp(index_dir=tmp_index_dir, config=load(config_file))
    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot, 5)
        wizard = await _wizard(app, pilot)
        wizard._open_filters()
        await _settle(pilot)
        browser: Any = app.screen
        browser._spec = dataclasses.replace(browser._spec, excludes=tuple(NODE), kinds=("md",))
        await pilot.press("escape")
        await _settle(pilot)
        wizard._fields["name"] = "research"
        wizard._fields["path"] = str(research)
        await pilot.press("ctrl+s")
        await _settle(pilot)
    source = load(config_file).collections["research"].sources[0]
    assert source.filters is not None
    assert source.filters.excludes == NODE
    assert source.filters.kinds == ["md"]


@pytest.mark.asyncio
async def test_an_untouched_new_collection_inherits_the_master_excludes(
    config_file: Path, tmp_index_dir: Path, tmp_path: Path
) -> None:
    """Nothing set in the wizard records nothing: the source follows the master list."""
    from fnd.config import write_settings

    write_settings(config_path=config_file, values={"defaults.filters.excludes": NODE})
    research = tmp_path / "research"
    research.mkdir()
    app = FNDApp(index_dir=tmp_index_dir, config=load(config_file))
    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(pilot, 5)
        wizard = await _wizard(app, pilot)
        wizard._fields["name"] = "research"
        wizard._fields["path"] = str(research)
        await pilot.press("ctrl+s")
        await _settle(pilot)
    source = load(config_file).collections["research"].sources[0]
    assert source.filters is None or source.filters.excludes is None
    assert source.excludes == NODE

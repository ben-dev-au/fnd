"""`?` described keys that do nothing, and left out a screen entirely.

Two of its claims were checked by pressing them: Tab on the source form does
not move focus while the frontmatter sample is hidden, and 1-9 types rather
than jumps because a settings screen opens with the filter box focused. The
Index-filters screen had no section at all: its keys are widget bindings, and
only the action registry and four hand-written tables reach the sheet.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from textual.widgets import Input, TextArea

from fnd.config import CollectionConfig, Config, SourceConfig, load, write_collection
from fnd.tui import FNDApp
from fnd.tui.menu import MenuItem, _provider_keybindings
from fnd.tui.settings_screen import SettingsList, SettingsScreen, SourceFormScreen


def _sheet() -> tuple[MenuItem, ...]:
    return _provider_keybindings(cast("Any", SimpleNamespace(_config=None)))


def _rows(section_label: str) -> list[tuple[str, str, str]]:
    """(key, label, description) for every row under a section heading."""
    out: list[tuple[str, str, str]] = []
    inside = False
    for item in _sheet():
        if item.is_header:
            inside = item.label == section_label
            continue
        if inside:
            out.append((item.key, item.label, item.description))
    return out


def _row_named(section: str, label_fragment: str) -> tuple[str, str, str]:
    return next(r for r in _rows(section) if label_fragment in r[1])


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg_path = tmp_path / "config.toml"
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    write_collection(
        config_path=cfg_path,
        name="probe",
        collection=CollectionConfig(sources=[SourceConfig(path=root)]),
    )
    return load(cfg_path)


@pytest.mark.asyncio
async def test_tab_does_nothing_until_there_is_a_sample(
    config: Config, tmp_index_dir: Path
) -> None:
    """The behaviour the row describes, both halves of it."""
    app = FNDApp(index_dir=tmp_index_dir, config=config)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.push_screen(SourceFormScreen(collection_name="probe", source_index=0))
        for _ in range(30):
            await pilot.pause()
        form = app.screen
        assert isinstance(form, SourceFormScreen)
        assert not form.query_one("#frontmatter_sample", TextArea).display

        await pilot.press("tab")
        await pilot.pause()
        with_no_rule = type(app.focused).__name__

        form._fields["filters"]["frontmatter"] = "Course == 'X'"
        form._populate_fields()
        for _ in range(10):
            await pilot.pause()
        form.query_one(SettingsList).focus()
        await pilot.press("tab")
        for _ in range(5):
            await pilot.pause()
        with_a_rule = type(app.focused).__name__

    assert with_no_rule == "SettingsList", "nothing to cycle to, so nothing moves"
    assert with_a_rule == "TextArea", "with a sample shown, Tab must reach it"


def test_the_tab_row_names_the_condition() -> None:
    _key, _label, description = _row_named("Source form", "Field list")
    assert "rule" in description, description


@pytest.mark.asyncio
async def test_the_number_keys_need_the_row_list(config: Config, tmp_index_dir: Path) -> None:
    from fnd.tui.settings_screen import open_settings

    app = FNDApp(index_dir=tmp_index_dir, config=config)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        open_settings(app)
        for _ in range(20):
            await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        assert isinstance(app.focused, Input), "it opens on the filter box"

        await pilot.press("3")
        await pilot.pause()
        typed = screen.query_one(Input).value

        screen.query_one(Input).value = ""  # unfilter, so the rows are all there
        screen.query_one(SettingsList).focus()
        await pilot.pause()
        await pilot.press("3")
        for _ in range(5):
            await pilot.pause()
        jumped = screen.query_one(SettingsList).cursor_index

    assert typed == "3", "digits reach the filter box, as the row now says"
    assert jumped > 0, "with the list focused they jump"


def test_the_number_row_names_the_filter_box() -> None:
    _key, _label, description = _row_named("Settings menu", "Jump by index")
    assert "filter box" in description, description


def _key_cells(section: str) -> set[str]:
    """The key column, split into the tokens a row actually offers.

    Substring-matching the joined column lets `"t" in "Enter"` pass, so deleting
    the `t` row would leave the guard green.
    """
    cells: set[str] = set()
    for key, _label, _description in _rows(section):
        # " / " with spaces: a row whose whole key IS `/` must survive.
        cells.update(part.strip() for part in key.split(" / ") if part.strip())
    return cells


def test_the_filter_browser_has_a_section() -> None:
    """Derived from the screen's own bindings, so the table cannot drift."""
    from textual.binding import Binding

    from fnd.tui.settings_screen import FilterBrowserScreen
    from fnd.tui.widgets import COMMIT_KEY

    listed = _key_cells("Index filters") | _key_cells("Global")
    pretty = {
        "escape": "Esc",
        "slash": "/",
        "ctrl+s": COMMIT_KEY,
        "question_mark": "?",
        "down": "↓",
    }
    for binding in FilterBrowserScreen.BINDINGS:
        assert isinstance(binding, Binding)
        first = binding.key.split(",")[0]
        token = pretty.get(first, first)
        assert token in listed, f"{binding.key} is documented nowhere ({sorted(listed)})"


def test_that_guard_can_actually_fail() -> None:
    """Deleting the row this guard exists for must turn it red."""
    assert "t" in _key_cells("Index filters"), "the row this guard exists for"
    assert "t" not in _key_cells("Global"), "a cell, not a substring of Enter"


def test_a_key_that_works_in_every_tree_is_listed_in_each() -> None:
    """It worked in all three trees and was documented under one."""
    for pane in ("Results pane", "Outline panel", "Filters panel", "Collections panel"):
        labels = {label for _k, label, _d in _rows(pane)}
        assert "Expand" in labels, f"{pane} does not name the key that expands its rows"
        assert "Collapse" in labels, f"{pane} does not name the key that collapses them"


def test_a_key_is_not_listed_where_it_does_not_work() -> None:
    """The control: repetition follows the contexts, it is not blanket."""
    labels = {label for _k, label, _d in _rows("Collections panel")}
    assert "Clear filters" not in labels, "clear_filters is results and filters only"


@pytest.mark.asyncio
async def test_help_from_the_filter_browser_lifts_its_section(
    config: Config, tmp_index_dir: Path
) -> None:
    from fnd.filters import FilterSpec
    from fnd.tui.settings_screen import FilterBrowserScreen

    app = FNDApp(index_dir=tmp_index_dir, config=config)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.push_screen(
            FilterBrowserScreen(
                title="Index filters",
                spec=FilterSpec(),
                gitignore=True,
                fndignore=True,
                on_save=lambda *_a: None,
            )
        )
        for _ in range(20):
            await pilot.pause()
        hint = app._keybindings_context_hint()

    assert hint == "Index filters"

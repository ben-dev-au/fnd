"""Every Settings hint names what its key does in the place it is shown."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input, Static

from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.settings_screen import SettingsList, SettingsScreen
from tests._pilot_wait import screen_ready, settings_ready, wait_until


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


def _footer(screen: Any) -> str:
    return str(screen.query_one("#footer_hints", Static).content)


def _shows(screen: Any, key: str, label: str) -> bool:
    return f" {key}  {label}" in _footer(screen)


async def _open_menu(pilot: Any, app: FNDApp, *, on_list: bool = True) -> SettingsScreen:
    """The menu, with focus moved from its filter box to the list unless asked not to."""
    await pilot.pause()
    await pilot.press("escape", "colon")
    screen = await settings_ready(pilot, app)
    if on_list:
        await pilot.press("down")
        await wait_until(pilot, lambda: isinstance(app.focused, SettingsList))
    return screen


@pytest.mark.asyncio
async def test_the_filter_box_says_esc_goes_back_while_empty(built_index: Path) -> None:
    app = FNDApp(index_dir=built_index)
    async with app.run_test(size=(140, 45)) as pilot:
        screen = await _open_menu(pilot, app, on_list=False)
        await wait_until(pilot, lambda: _shows(screen, "Esc", "Back"))
        await pilot.press("i", "n")
        await wait_until(pilot, lambda: _shows(screen, "Esc", "Clear"))


@pytest.mark.asyncio
async def test_colon_on_a_settings_page_is_named_for_closing_it(built_index: Path) -> None:
    app = FNDApp(index_dir=built_index)
    async with app.run_test(size=(140, 45)) as pilot:
        screen = await _open_menu(pilot, app)
        await wait_until(pilot, lambda: _shows(screen, ":", "Close"))
        await pilot.press("colon")
        await wait_until(pilot, lambda: not isinstance(app.screen, SettingsScreen))


@pytest.mark.asyncio
async def test_the_keybindings_sheet_offers_enter_only_on_rows_that_run(built_index: Path) -> None:
    app = FNDApp(index_dir=built_index)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        await pilot.press("escape", "question_mark")
        screen = await settings_ready(pilot, app)
        lst = screen.query_one(SettingsList)
        rows = [i for i, item in enumerate(lst._items) if item.kind != "header"]
        doc = next(i for i in rows if not lst._items[i].action_id)
        runs = next(i for i in rows if lst._items[i].action_id)
        lst.cursor_index = doc
        await wait_until(pilot, lambda: not _shows(screen, "⏎", "Run"))
        lst.cursor_index = runs
        await wait_until(pilot, lambda: _shows(screen, "⏎", "Run"))


@pytest.mark.asyncio
async def test_question_mark_closes_the_keybindings_sheet(built_index: Path) -> None:
    app = FNDApp(index_dir=built_index)
    async with app.run_test(size=(140, 45)) as pilot:
        screen = await _open_menu(pilot, app)
        await pilot.press("question_mark")
        await wait_until(pilot, lambda: app.screen is not screen)
        keys = await screen_ready(pilot, app)
        assert _shows(keys, "?", "Close")
        await pilot.press("question_mark")
        await wait_until(pilot, lambda: app.screen is screen)


@pytest.fixture
def one_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fnd.config import CollectionConfig, SourceConfig, write_collection

    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    notes = tmp_path / "notes"
    notes.mkdir()
    write_collection(
        config_path=cfg_path,
        name="probe",
        collection=CollectionConfig(sources=[SourceConfig(path=notes)]),
    )


async def _open_form(pilot: Any, app: FNDApp, screen: Any) -> Any:
    from fnd.config import load

    await pilot.pause()
    app._config = load()
    app.push_screen(screen)
    return await screen_ready(pilot, app, type(screen))


def _put_cursor_on(screen: Any, item_id: str) -> None:
    lst = screen.query_one(SettingsList)
    lst.cursor_index = next(i for i, it in enumerate(lst._items) if it.id == item_id)


@pytest.mark.asyncio
async def test_a_source_form_names_the_edit_bar_keys_while_it_is_open(
    built_index: Path, one_source: None
) -> None:
    from fnd.tui.settings_screen import SourceFormScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test(size=(140, 45)) as pilot:
        form = await _open_form(
            pilot, app, SourceFormScreen(collection_name="probe", source_index=0)
        )
        assert _shows(form, "Ctrl+D", "Delete source")
        _put_cursor_on(form, "form.path")
        await pilot.press("enter")
        await wait_until(pilot, lambda: _shows(form, "⏎", "Set"))
        assert _shows(form, "Esc", "Cancel")
        assert "Delete source" not in _footer(form)
        assert "Quit" not in _footer(form)
        await pilot.press("escape")
        await wait_until(pilot, lambda: _shows(form, "Ctrl+D", "Delete source"))


@pytest.mark.asyncio
async def test_the_wizard_names_tab_once_a_rule_gives_it_a_sample_to_test(
    built_index: Path, one_source: None
) -> None:
    from textual.widgets import Input

    from fnd.tui.settings_screen import AddCollectionWizard, EditBar

    app = FNDApp(index_dir=built_index)
    async with app.run_test(size=(140, 45)) as pilot:
        wizard = await _open_form(pilot, app, AddCollectionWizard())
        assert "Test a sample" not in _footer(wizard)
        _put_cursor_on(wizard, "wiz.filter")
        await pilot.press("enter")
        await wait_until(pilot, lambda: _shows(wizard, "⏎", "Set"))
        wizard.query_one(EditBar).query_one(Input).value = "draft == true"
        await pilot.press("enter")
        await wait_until(pilot, lambda: _shows(wizard, "Tab", "Test a sample"))
        await pilot.press("tab")
        await wait_until(pilot, lambda: _shows(wizard, "Tab", "Fields"))
        assert "⏎" not in _footer(wizard)


@pytest.mark.asyncio
async def test_the_unsaved_changes_question_names_no_key_it_overrides(
    built_index: Path, one_source: None
) -> None:
    from fnd.tui.settings_screen import SourceFormScreen, UnsavedChangesScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test(size=(140, 45)) as pilot:
        form = await _open_form(
            pilot, app, SourceFormScreen(collection_name="probe", source_index=0)
        )
        form._fields["label"] = "changed"
        await pilot.press("escape")
        await wait_until(pilot, lambda: isinstance(app.screen, UnsavedChangesScreen))
        question = app.screen
        assert _shows(question, "Esc", "Keep editing")
        assert "Quit" not in _footer(question)
        assert "Menu" not in _footer(question)


async def _open_filters(pilot: Any, app: FNDApp) -> Any:
    from fnd.tui.settings_screen import FilterBrowserScreen, SourceFormScreen

    form = await _open_form(pilot, app, SourceFormScreen(collection_name="probe", source_index=0))
    _put_cursor_on(form, "form.filters")
    await pilot.press("right")
    await wait_until(
        pilot,
        lambda: isinstance(app.screen, FilterBrowserScreen) and not app.screen._scanning,
    )
    return app.screen


def _cursor_on_row(browser: Any, starts: str) -> None:
    from fnd.tui.widgets.toggle_tree import ToggleTree

    tree = browser.query_one("#filter_tree", ToggleTree)
    tree.cursor_line = next(
        line
        for line in range(tree.last_line + 1)
        if (node := tree.get_node_at_line(line)) is not None
        and str(node.label).lstrip("●◐○⊘ ").startswith(starts)
    )


@pytest.mark.asyncio
async def test_the_filter_browser_names_what_enter_does_on_each_row(
    built_index: Path, one_source: None
) -> None:
    app = FNDApp(index_dir=built_index)
    async with app.run_test(size=(160, 50)) as pilot:
        browser = await _open_filters(pilot, app)
        _cursor_on_row(browser, "Rules you type")
        await wait_until(pilot, lambda: _shows(browser, "⏎", "Expand"))
        assert _shows(browser, "→", "Expand")
        assert _shows(browser, "Esc/←", "Back")
        await pilot.press("enter")
        await wait_until(pilot, lambda: _shows(browser, "⏎", "Collapse"))
        assert _shows(browser, "←", "Collapse")
        await pilot.press("down")
        await wait_until(pilot, lambda: _shows(browser, "⏎", "Edit"))
        assert _shows(browser, "←", "Parent")
        _cursor_on_row(browser, "Obey ignore files")
        await wait_until(pilot, lambda: _shows(browser, "⏎", "Toggle"))


@pytest.mark.asyncio
async def test_the_filter_browser_offers_return_to_defaults_once_a_tick_departs(
    built_index: Path, one_source: None
) -> None:
    app = FNDApp(index_dir=built_index)
    async with app.run_test(size=(160, 50)) as pilot:
        browser = await _open_filters(pilot, app)
        assert "Return to defaults" not in _footer(browser)
        _cursor_on_row(browser, "Obey ignore files")
        await pilot.press("enter")
        await wait_until(pilot, lambda: "Return to defaults" in _footer(browser))
        await pilot.press("enter")
        await wait_until(pilot, lambda: "Return to defaults" not in _footer(browser))


@pytest.mark.asyncio
async def test_the_filter_browser_search_says_esc_leaves_while_empty(
    built_index: Path, one_source: None
) -> None:
    app = FNDApp(index_dir=built_index)
    async with app.run_test(size=(160, 50)) as pilot:
        browser = await _open_filters(pilot, app)
        await pilot.press("slash")
        await wait_until(pilot, lambda: _shows(browser, "Esc", "Back"))
        await pilot.press("t")
        await wait_until(pilot, lambda: _shows(browser, "Esc", "Clear"))
        await pilot.press("enter")
        await wait_until(pilot, lambda: "Clear" in _footer(browser))
        assert "Back" not in _footer(browser)


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [[], [("default", "/flat.pdf", "still flat", None)]])
async def test_the_flat_pdf_list_names_row_keys_only_when_it_has_rows(
    built_index: Path, monkeypatch: pytest.MonkeyPatch, rows: list[Any]
) -> None:
    from fnd.tui import flat_pdf_scan
    from fnd.tui.settings_screen import StillFlatDrillIn

    flat_pdf_scan.invalidate_all()
    monkeypatch.setattr(
        "fnd.tui.settings_screen._flat_pdfs_with_reasons", lambda *, collection=None: rows
    )
    app = FNDApp(index_dir=built_index)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        await app.push_screen(StillFlatDrillIn(collection=None))
        screen = app.screen
        await wait_until(pilot, lambda: _shows(screen, "⏎ / r", "Retry") == bool(rows))
        assert _shows(screen, "Esc", "Back")


@pytest.mark.asyncio
async def test_a_source_form_names_tab_as_its_rule_comes_and_goes(
    built_index: Path, one_source: None
) -> None:
    from fnd.tui.settings_screen import SourceFormScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test(size=(140, 45)) as pilot:
        form = await _open_form(
            pilot, app, SourceFormScreen(collection_name="probe", source_index=0)
        )
        assert "Test a sample" not in _footer(form)
        form._fields["filters"] = {**form._fields["filters"], "frontmatter": "draft == true"}
        form._refresh_sample_tester()
        assert _shows(form, "Tab", "Test a sample")
        form._fields["filters"] = {**form._fields["filters"], "frontmatter": ""}
        form._refresh_sample_tester()
        assert "Test a sample" not in _footer(form)


@pytest.mark.asyncio
async def test_the_keybindings_filter_keeps_enter_and_colon_honest(built_index: Path) -> None:
    app = FNDApp(index_dir=built_index)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        await pilot.press("escape", "question_mark")
        sheet = await settings_ready(pilot, app)
        await pilot.press("slash", *"toml", "down")
        lst = sheet.query_one(SettingsList)
        await wait_until(pilot, lambda: isinstance(app.focused, SettingsList))
        assert lst._items[lst.cursor_index].kind == "external"
        await wait_until(pilot, lambda: _shows(sheet, "⏎", "Open in editor"))
        assert _shows(sheet, ":", "Menu")
        await pilot.press("colon")
        await wait_until(pilot, lambda: app.screen is not sheet)
        root = await settings_ready(pilot, app)
        assert not root._breadcrumb


@pytest.mark.asyncio
async def test_the_filter_browser_footer_follows_the_rebuilt_tree(
    built_index: Path, one_source: None
) -> None:
    app = FNDApp(index_dir=built_index)
    async with app.run_test(size=(160, 50)) as pilot:
        browser = await _open_filters(pilot, app)
        _cursor_on_row(browser, "File types")
        await pilot.press("enter")
        await wait_until(pilot, lambda: _shows(browser, "Esc/←", "Back"))
        assert "Parent" not in _footer(browser)
        _cursor_on_row(browser, "Obey ignore files")
        await pilot.press("enter")
        await wait_until(pilot, lambda: "Return to defaults" in _footer(browser))
        browser.action_clear_all()
        await wait_until(pilot, lambda: "Return to defaults" not in _footer(browser))


@pytest.mark.asyncio
async def test_a_documenting_key_row_offers_no_enter_through_the_menu_filter(
    built_index: Path,
) -> None:
    app = FNDApp(index_dir=built_index)
    async with app.run_test(size=(140, 45)) as pilot:
        screen = await _open_menu(pilot, app, on_list=False)
        lst = screen.query_one(SettingsList)
        screen.query_one("#settings_search", Input).value = "no preview load"
        await wait_until(pilot, lambda: any(i.key and not i.action_id for i in lst._items))
        await pilot.press("down")
        await wait_until(pilot, lambda: isinstance(app.focused, SettingsList))
        lst.cursor_index = next(
            i for i, item in enumerate(lst._items) if item.key and not item.action_id
        )
        await wait_until(pilot, lambda: _shows(screen, "↑↓", "Nav"))
        assert "⏎" not in _footer(screen)


@pytest.mark.asyncio
async def test_esc_and_left_say_they_clear_a_filtered_list(built_index: Path) -> None:
    app = FNDApp(index_dir=built_index)
    async with app.run_test(size=(140, 45)) as pilot:
        menu = await _open_menu(pilot, app, on_list=False)
        await pilot.press(*"index", "down")
        await wait_until(pilot, lambda: _shows(menu, "←", "Clear"))
        await pilot.press("left")
        await wait_until(pilot, lambda: _shows(menu, "←", "Back"))
        await pilot.press("question_mark")
        await wait_until(pilot, lambda: app.screen is not menu)
        sheet = await settings_ready(pilot, app)
        await pilot.press("slash", *"quit", "down")
        await wait_until(pilot, lambda: _shows(sheet, "Esc", "Clear"))
        await pilot.press("escape")
        await wait_until(pilot, lambda: _shows(sheet, "Esc", "Back"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("branches", "label"),
    [(("Maximum file size",), "Select"), (("Tags", "Note tags"), "Cycle")],
)
async def test_the_filter_browser_names_enter_on_an_option_by_what_it_does(
    built_index: Path, one_source: None, branches: tuple[str, ...], label: str
) -> None:
    app = FNDApp(index_dir=built_index)
    async with app.run_test(size=(160, 50)) as pilot:
        browser = await _open_filters(pilot, app)
        for branch in branches:
            _cursor_on_row(browser, branch)
            await pilot.press("enter")
            await wait_until(pilot, lambda: _shows(browser, "⏎", "Collapse"))
        await pilot.press("down")
        await wait_until(pilot, lambda: _shows(browser, "⏎", label))

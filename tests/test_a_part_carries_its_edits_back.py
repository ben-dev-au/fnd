"""A part edits its document: Esc carries the edit back, with no ^s and no prompt."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, cast

import pytest
from textual.widgets import TextArea

from fnd.config import CollectionConfig, SourceConfig, load, write_collection
from fnd.index_freshness import Ledger, indexed_with
from fnd.tui import FNDApp
from fnd.tui.settings_screen import (
    FilterBrowserScreen,
    SourceFormScreen,
    UnsavedChangesScreen,
)


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
def index_dir(tmp_path: Path, config_file: Path) -> Path:
    path = tmp_path / "idx"
    path.mkdir()
    cfg = load(config_file)
    Ledger(path).record("notes", indexed_with(cfg.collections["notes"], cfg.defaults))
    return path


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(
        "fnd.tui.indexer_service.IndexerService.reindex_with_warning",
        lambda _self, name, **_kw: calls.append(name),
    )
    return calls


async def _settle(pilot: Any, n: int = 20) -> None:
    for _ in range(n):
        await pilot.pause()


def _stack(app: FNDApp) -> list[str]:
    return [type(s).__name__ for s in app.screen_stack]


def _footer(app: FNDApp) -> str:
    rows = app.screen._compositor.render_strips()
    return "".join(seg.text for seg in rows[-1])


async def _source_filters(app: FNDApp, pilot: Any) -> tuple[SourceFormScreen, FilterBrowserScreen]:
    app.push_screen(SourceFormScreen(collection_name="notes", source_index=0))
    await _settle(pilot)
    form = app.screen
    assert isinstance(form, SourceFormScreen)
    form._open_filters()
    await _settle(pilot)
    browser = app.screen
    assert isinstance(browser, FilterBrowserScreen)
    return form, browser


def _exclude_draft(browser: FilterBrowserScreen) -> None:
    tags: dict[str, Any] = dict(cast("Any", browser._spec.exclude_tags))
    tags["frontmatter"] = (*tags.get("frontmatter", ()), "draft")
    browser._spec = dataclasses.replace(browser._spec, exclude_tags=tags)


@pytest.mark.asyncio
async def test_esc_on_the_source_filters_carries_the_edit_into_the_form(
    config_file: Path, index_dir: Path
) -> None:
    """One Esc returns to the form holding the change, with no prompt on the way."""
    app = FNDApp(index_dir=index_dir, config=load(config_file))
    async with app.run_test(size=(110, 30)) as pilot:
        await _settle(pilot, 5)
        form, browser = await _source_filters(app, pilot)
        _exclude_draft(browser)
        await pilot.press("escape")
        await _settle(pilot, 8)
        on_form = app.screen is form
        names = _stack(app)
        overrides = dict(form._fields["filters"])
    assert on_form, names
    assert UnsavedChangesScreen.__name__ not in names
    assert "draft" in overrides.get("exclude_tags", {}).get("frontmatter", ()), overrides


@pytest.mark.asyncio
async def test_the_source_filters_offer_no_save(config_file: Path, index_dir: Path) -> None:
    """A ^s hint there would say that Esc does not keep the edit."""
    app = FNDApp(index_dir=index_dir, config=load(config_file))
    async with app.run_test(size=(110, 30)) as pilot:
        await _settle(pilot, 5)
        _form, browser = await _source_filters(app, pilot)
        _exclude_draft(browser)
        browser._render_footer()
        await _settle(pilot, 3)
        footer = _footer(app)
        before = config_file.read_text(encoding="utf-8")
        await pilot.press("ctrl+s")
        await _settle(pilot, 5)
        after = config_file.read_text(encoding="utf-8")
    assert "^s" not in footer, footer
    assert "Back" in footer, footer
    assert before == after


@pytest.mark.asyncio
async def test_a_global_filter_save_writes_and_indexes_nothing(
    config_file: Path, index_dir: Path, started: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The global document saves config only and names the collections now out of date."""
    from fnd.tui.menu import _open_filter_browser
    from fnd.tui.settings_screen import UpdateAllConfirm

    app = FNDApp(index_dir=index_dir, config=load(config_file))
    seen: list[str] = []
    async with app.run_test(size=(110, 30)) as pilot:
        await _settle(pilot, 5)
        real = app.notify
        monkeypatch.setattr(
            app, "notify", lambda m, *a, **k: (seen.append(str(m)), real(m, *a, **k))
        )
        _open_filter_browser(app)
        await _settle(pilot)
        browser = app.screen
        assert isinstance(browser, FilterBrowserScreen)
        _exclude_draft(browser)
        await pilot.press("ctrl+s")
        await _settle(pilot, 10)
        names = _stack(app)
    assert "draft" in cast("Any", load(config_file).defaults.filters.exclude_tags)["frontmatter"]
    assert started == []
    assert UpdateAllConfirm.__name__ not in names
    assert any("is outdated" in m and "press u" in m for m in seen), seen


@pytest.mark.asyncio
async def test_esc_on_valid_filter_text_carries_it_back(config_file: Path, index_dir: Path) -> None:
    """The text view is a part of the browser: Esc hands the parsed set back."""
    app = FNDApp(index_dir=index_dir, config=load(config_file))
    async with app.run_test(size=(110, 30)) as pilot:
        await _settle(pilot, 5)
        _form, browser = await _source_filters(app, pilot)
        browser.action_edit_text()
        await _settle(pilot)
        box = app.screen.query_one("#filter_text", TextArea)
        box.text = "file.size <= 1000"
        await _settle(pilot, 4)
        await pilot.press("escape")
        await _settle(pilot, 8)
        back = app.screen is browser
        names = _stack(app)
        bound = browser._spec.max_size
    assert back, names
    assert UnsavedChangesScreen.__name__ not in names
    assert bound == 1000


@pytest.mark.asyncio
async def test_an_invalid_rule_holds_out_loud_then_a_second_esc_cancels(
    config_file: Path, index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken rule is never passed on, the refusal is said, and the user is never trapped."""
    from fnd.tui.settings_screen import RuleTextScreen

    app = FNDApp(index_dir=index_dir, config=load(config_file))
    seen: list[str] = []
    async with app.run_test(size=(110, 30)) as pilot:
        await _settle(pilot, 5)
        _form, browser = await _source_filters(app, pilot)
        before = browser._spec.frontmatter
        app.push_screen(
            RuleTextScreen(
                title="Frontmatter rule",
                value="",
                note_scoped=True,
                on_save=lambda text: setattr(
                    browser, "_spec", dataclasses.replace(browser._spec, frontmatter=text)
                ),
            )
        )
        await _settle(pilot)
        rule = app.screen
        real = app.notify
        monkeypatch.setattr(
            app, "notify", lambda m, *a, **k: (seen.append(str(m)), real(m, *a, **k))
        )
        rule.query_one("#rule_text", TextArea).text = "status == ("
        await _settle(pilot, 4)
        await pilot.press("escape")
        await _settle(pilot, 5)
        held = app.screen is rule
        footer = _footer(app)
        await pilot.press("escape")
        await _settle(pilot, 5)
        returned = app.screen is browser
        after = browser._spec.frontmatter
    assert held
    assert seen, seen
    assert "col" in seen[-1], seen
    assert "Cancel" in footer, footer
    assert returned
    assert after == before


@pytest.mark.asyncio
async def test_fixing_the_typing_carries_the_fixed_value(
    config_file: Path, index_dir: Path
) -> None:
    """A hold is re-armed by new typing, so the corrected rule goes back."""
    from fnd.tui.settings_screen import RuleTextScreen

    app = FNDApp(index_dir=index_dir, config=load(config_file))
    got: list[str] = []
    async with app.run_test(size=(110, 30)) as pilot:
        await _settle(pilot, 5)
        app.push_screen(
            RuleTextScreen(title="Rule", value="", note_scoped=True, on_save=got.append)
        )
        await _settle(pilot)
        rule = app.screen
        box = rule.query_one("#rule_text", TextArea)
        box.text = "status == ("
        await _settle(pilot, 4)
        await pilot.press("escape")
        await _settle(pilot, 4)
        box.text = "status == 'done'"
        await _settle(pilot, 4)
        await pilot.press("escape")
        await _settle(pilot, 5)
        gone = app.screen is not rule
    assert gone
    assert got == ["status == 'done'"]


@pytest.mark.asyncio
async def test_coming_back_from_a_part_keeps_the_row_it_was_opened_from(
    config_file: Path, index_dir: Path
) -> None:
    """Esc returns to the Index filters row, not the top: the next Enter reopens it."""
    from fnd.tui.settings_screen import SettingsList

    app = FNDApp(index_dir=index_dir, config=load(config_file))
    async with app.run_test(size=(110, 30)) as pilot:
        await _settle(pilot, 5)
        app.push_screen(SourceFormScreen(collection_name="notes", source_index=0))
        await _settle(pilot)
        form = app.screen
        assert isinstance(form, SourceFormScreen)
        lst = form.query_one(SettingsList)
        lst.cursor_index = next(i for i, it in enumerate(lst._items) if it.id == "form.filters")
        await _settle(pilot, 3)
        await pilot.press("enter")
        await _settle(pilot)
        browser = app.screen
        assert isinstance(browser, FilterBrowserScreen)
        _exclude_draft(browser)
        await pilot.press("escape")
        await _settle(pilot, 8)
        assert app.screen is form
        row = lst._items[lst.cursor_index].id
    assert row == "form.filters", row


@pytest.mark.asyncio
async def test_visiting_a_sources_filters_and_changing_nothing_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, index_dir: Path
) -> None:
    """An override equal to its default stays as written, and quitting asks nothing."""
    cfg_path = tmp_path / "config.toml"
    write_collection(
        config_path=cfg_path,
        name="notes",
        collection=CollectionConfig(
            sources=[
                SourceConfig(
                    path=tmp_path / "notes",
                    filters=cast("Any", {"respect_gitignore": True}),
                )
            ]
        ),
    )
    app = FNDApp(index_dir=index_dir, config=load(cfg_path))
    async with app.run_test(size=(110, 30)) as pilot:
        await _settle(pilot, 5)
        form, _browser = await _source_filters(app, pilot)
        before = dict(form._fields["filters"])
        await pilot.press("escape")
        await _settle(pilot, 8)
        after = dict(form._fields["filters"])
        dirty = form.is_dirty()
    assert after == before
    assert not dirty


@pytest.mark.asyncio
async def test_a_refused_save_from_the_prompt_is_said_out_loud(
    config_file: Path, index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Saving a form hidden under a part refuses where the user can see it."""
    from textual.widgets import OptionList

    app = FNDApp(index_dir=index_dir, config=load(config_file))
    seen: list[str] = []
    async with app.run_test(size=(110, 30)) as pilot:
        await _settle(pilot, 5)
        form, browser = await _source_filters(app, pilot)
        _exclude_draft(browser)
        monkeypatch.setattr(form, "write", lambda: "the config on disk changed")
        real = app.notify
        monkeypatch.setattr(
            app, "notify", lambda m, *a, **k: (seen.append(str(m)), real(m, *a, **k))
        )
        app.action_quit()
        await _settle(pilot, 8)
        options = app.screen.query_one("#confirm_list", OptionList)
        options.highlighted = 0
        options.action_select()
        await _settle(pilot, 8)
        running = app.is_running
    assert running
    assert any("the config on disk changed" in m for m in seen), seen

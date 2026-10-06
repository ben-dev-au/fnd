"""Excluded paths live on the Index filters screen: presets to tick, globs to type."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import TextArea

from fnd.config import EXCLUDES_PRESETS
from fnd.filters import FilterSpec
from fnd.filters.tree_model import apply_selection, selection_for, spec_branches
from fnd.tui import FNDApp

NODE = EXCLUDES_PRESETS["node_modules"]["globs"]
HIDDEN = EXCLUDES_PRESETS["hidden"]["globs"]


def _branch(spec: FilterSpec, branch_id: str) -> Any:
    return next(b for b in spec_branches(spec) if b.id == branch_id)


def test_the_branch_offers_every_preset() -> None:
    """One row per preset, after the ignore files."""
    ids = [b.id for b in spec_branches(FilterSpec())]
    assert ids.index("excludes") == ids.index("ignore") + 1
    items = [i[0] for i in _branch(FilterSpec(), "excludes").items]
    assert items == [f"exclude:{key}" for key in EXCLUDES_PRESETS]


def test_a_preset_is_ticked_when_all_its_globs_are_excluded() -> None:
    """The form's old rule: a preset is on when every glob it ships is present."""
    selected, _excluded = selection_for(FilterSpec(excludes=(*NODE, "x/**")))
    assert "exclude:node_modules" in selected
    assert "exclude:hidden" not in selected


def test_ticking_presets_keeps_typed_globs() -> None:
    """Toggling a preset never drops a glob typed by hand outside it; the new globs follow."""
    spec = FilterSpec(excludes=("x/**",))
    selected, excluded = selection_for(spec)
    updated, _git, _fnd = apply_selection(
        spec, selected | {"exclude:hidden", "exclude:node_modules"}, excluded
    )
    assert updated.excludes == ("x/**", *HIDDEN, *NODE)


def test_a_change_elsewhere_leaves_the_list_as_written() -> None:
    """Rewriting an inherited list in registry order would save it as the source's own."""
    written = FilterSpec(excludes=("archive/**", *NODE, *HIDDEN))
    selected, excluded = selection_for(written)
    ticked, _g, _f = apply_selection(written, selected | {"kind:md"}, excluded)
    back, _g, _f = apply_selection(ticked, selected, excluded)
    assert ticked.excludes == written.excludes
    assert back == written


def test_unticking_a_preset_keeps_the_order_written() -> None:
    """Only the unticked preset's globs leave; the rest stay where the user put them."""
    written = FilterSpec(excludes=("b/**", *NODE, "a/**", *HIDDEN))
    selected, excluded = selection_for(written)
    updated, _g, _f = apply_selection(written, selected - {"exclude:node_modules"}, excluded)
    assert updated.excludes == ("b/**", "a/**", *HIDDEN)


def test_unticking_a_preset_removes_only_its_globs() -> None:
    """The other excludes stay."""
    spec = FilterSpec(excludes=(*NODE, "x/**"))
    selected, excluded = selection_for(spec)
    updated, _git, _fnd = apply_selection(spec, selected - {"exclude:node_modules"}, excluded)
    assert updated.excludes == ("x/**",)


def test_typed_globs_are_a_rule_you_type() -> None:
    """Globs beyond the presets are typed, beside the frontmatter and custom rules."""
    rules = _branch(FilterSpec(excludes=(*NODE, "x/**", "y/**")), "rules")
    row = next(i for i in rules.items if i[0] == "rule:excludes")
    assert row[1] == "Excluded globs   x/**, y/**"


def test_the_text_view_never_shows_excludes() -> None:
    """They prune folders before a file is read, which no expression can say."""
    from fnd.filters.text_form import render

    assert render(FilterSpec(excludes=("x/**",))) == ""


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from fnd.config import CollectionConfig, SourceConfig, write_collection

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


async def _global_filters(app: FNDApp, pilot: Any) -> Any:
    from fnd.tui.menu import _open_filter_browser

    _open_filter_browser(app)
    await _settle(pilot)
    return app.screen


@pytest.mark.asyncio
async def test_the_text_view_round_trip_keeps_excludes(
    config_file: Path, tmp_index_dir: Path
) -> None:
    """Editing the expression as text never drops the excluded paths."""
    from fnd.config import load

    app = FNDApp(index_dir=tmp_index_dir, config=load(config_file))
    async with app.run_test(size=(120, 34)) as pilot:
        await _settle(pilot, 5)
        browser = await _global_filters(app, pilot)
        browser._spec = dataclasses.replace(browser._spec, excludes=("x/**",))
        browser.action_edit_text()
        await _settle(pilot)
        app.screen.query_one("#filter_text", TextArea).text = "file.size <= 1000"
        await _settle(pilot, 4)
        await pilot.press("escape")
        await _settle(pilot)
        kept = browser._spec.excludes
    assert kept == ("x/**",)


@pytest.mark.asyncio
async def test_a_global_save_writes_the_master_excludes(
    config_file: Path, tmp_index_dir: Path
) -> None:
    """Ticking a preset on the global screen and saving writes [defaults.filters] excludes."""
    from fnd.config import load

    app = FNDApp(index_dir=tmp_index_dir, config=load(config_file))
    async with app.run_test(size=(120, 34)) as pilot:
        await _settle(pilot, 5)
        browser = await _global_filters(app, pilot)
        browser._spec = dataclasses.replace(browser._spec, excludes=tuple(NODE))
        await pilot.press("ctrl+s")
        await _settle(pilot)
    assert load(config_file).defaults.filters.excludes == NODE


@pytest.mark.asyncio
async def test_typed_globs_hold_on_an_invalid_one_then_cancel(
    config_file: Path, tmp_index_dir: Path
) -> None:
    """The glob editor is a part: an invalid glob holds the user, a second Esc abandons it."""
    from fnd.config import load
    from fnd.tui.settings_screen import GlobTextScreen

    app = FNDApp(index_dir=tmp_index_dir, config=load(config_file))
    got: list[tuple[str, ...]] = []
    async with app.run_test(size=(120, 34)) as pilot:
        await _settle(pilot, 5)
        app.push_screen(GlobTextScreen(title="Excluded globs", value=(), on_save=got.append))
        await _settle(pilot)
        editor = app.screen
        editor.query_one("#glob_text", TextArea).text = "a/**, b/[z-a]"
        await _settle(pilot, 4)
        await pilot.press("escape")
        await _settle(pilot, 4)
        held = app.screen is editor
        await pilot.press("escape")
        await _settle(pilot, 4)
        gone = app.screen is not editor
    assert held
    assert gone
    assert got == []


@pytest.mark.asyncio
async def test_typed_globs_carry_back_on_esc(config_file: Path, tmp_index_dir: Path) -> None:
    """Valid globs hand back as a tuple, trimmed, without empties."""
    from fnd.config import load
    from fnd.tui.settings_screen import GlobTextScreen

    app = FNDApp(index_dir=tmp_index_dir, config=load(config_file))
    got: list[tuple[str, ...]] = []
    async with app.run_test(size=(120, 34)) as pilot:
        await _settle(pilot, 5)
        app.push_screen(GlobTextScreen(title="Excluded globs", value=(), on_save=got.append))
        await _settle(pilot)
        app.screen.query_one("#glob_text", TextArea).text = " a/** ,, b/*.tmp "
        await _settle(pilot, 4)
        await pilot.press("escape")
        await _settle(pilot, 4)
    assert got == [("a/**", "b/*.tmp")]


@pytest.mark.asyncio
async def test_the_source_form_edits_excludes_in_its_index_filters(
    config_file: Path, tmp_index_dir: Path
) -> None:
    """No Excludes row on the form; a preset ticked in Index filters saves as the source's own."""
    from fnd.config import load
    from fnd.tui.settings_screen import SettingsList, SourceFormScreen

    app = FNDApp(index_dir=tmp_index_dir, config=load(config_file))
    async with app.run_test(size=(120, 34)) as pilot:
        await _settle(pilot, 5)
        app.push_screen(SourceFormScreen(collection_name="notes", source_index=0))
        await _settle(pilot)
        form: Any = app.screen
        rows = [i.id for i in form.query_one(SettingsList)._items]
        form._open_filters()
        await _settle(pilot)
        browser: Any = app.screen
        browser._spec = dataclasses.replace(browser._spec, excludes=tuple(NODE))
        await pilot.press("escape")
        await _settle(pilot)
        await pilot.press("ctrl+s")
        await _settle(pilot)
    assert "form.excludes" not in rows
    source = load(config_file).collections["notes"].sources[0]
    assert source.filters is not None
    assert source.filters.excludes == NODE


def test_the_branch_has_its_own_legend() -> None:
    """Ticked here means skipped, so the shared "index ONLY these" line would be false."""
    from fnd.filters.tree_model import EXCLUDES_LEGEND

    assert _branch(FilterSpec(), "excludes").legend == EXCLUDES_LEGEND
    assert "never read" in EXCLUDES_LEGEND


def test_the_filters_hints_name_excluded_paths() -> None:
    """Both rows into the screen list what is on it."""
    from fnd.tui.menu import SECTION_FILTERS, _provider_index_filters, _provider_root

    app: Any = None
    root = [i for i in _provider_root(app) if i.id == f"root.{SECTION_FILTERS}"]
    rows = [*_provider_index_filters(app), *root]
    assert len(rows) == 2
    assert all("excluded paths" in (r.description or "") for r in rows)


@pytest.mark.asyncio
async def test_the_excluded_globs_row_keeps_the_ticked_presets(
    config_file: Path, tmp_index_dir: Path
) -> None:
    """Typing globs on the row replaces the typed ones only."""
    from types import SimpleNamespace

    from fnd.config import load

    app = FNDApp(index_dir=tmp_index_dir, config=load(config_file))
    async with app.run_test(size=(120, 34)) as pilot:
        await _settle(pilot, 5)
        browser = await _global_filters(app, pilot)
        browser._spec = dataclasses.replace(browser._spec, excludes=(*NODE, "old/**"))
        browser._on_rule_selected(SimpleNamespace(item_id="rule:excludes"))
        await _settle(pilot)
        app.screen.query_one("#glob_text", TextArea).text = "new/**"
        await _settle(pilot, 4)
        await pilot.press("escape")
        await _settle(pilot)
        saved = browser._spec.excludes
    assert saved == (*NODE, "new/**")


def test_the_global_summary_names_excluded_paths() -> None:
    """A master list in force shows on the Index filters row."""
    from types import SimpleNamespace

    from fnd.config import DefaultFilters
    from fnd.tui.menu import _summary_index_filters

    app: Any = SimpleNamespace(
        _config=SimpleNamespace(defaults=SimpleNamespace(filters=DefaultFilters(excludes=NODE)))
    )
    assert "excluded paths" in _summary_index_filters(app)


def test_the_branch_names_presets_as_the_screen_does() -> None:
    """A collapsed branch reads "(System files)", never the config key "hidden"."""
    keys = [i[2] for i in _branch(FilterSpec(), "excludes").items]
    assert keys[:2] == ["System files", "Node modules"]


def test_returning_to_defaults_names_a_dropped_excludes_override() -> None:
    """The master is never empty now, so "dropped" means changed, not emptied."""
    from fnd.tui.settings_screen import _cleared_note

    note = _cleared_note(FilterSpec(excludes=tuple(NODE)), FilterSpec(excludes=tuple(HIDDEN)))
    assert note != "Nothing to return"
    assert "excluded paths" in note

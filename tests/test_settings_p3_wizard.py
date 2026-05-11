"""Phase 3 (Settings UX redesign) — Add Collection wizard tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from acorn.tui import AcornApp


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    from acorn.index import build_index

    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


def test_excludes_presets_exposed() -> None:
    """Spec: Wizard › Excludes — preset patterns, with safe defaults."""
    from acorn.config import EXCLUDES_PRESETS

    assert "hidden" in EXCLUDES_PRESETS
    hidden = EXCLUDES_PRESETS["hidden"]
    assert hidden["label"] == "Hidden / system"
    assert any(".git" in g for g in hidden["globs"])
    assert hidden["default"] is True  # pre-ticked
    assert "node_modules" in EXCLUDES_PRESETS
    assert EXCLUDES_PRESETS["node_modules"]["default"] is False


@pytest.mark.asyncio
async def test_add_collection_pushes_wizard_with_expected_fields(built_index: Path) -> None:
    """Spec: Wizard › Single screen — Name, Source path, Includes,
    Excludes, Frontmatter filter, Follow symlinks, plus the sample tester."""
    from acorn.tui import AcornApp
    from acorn.tui.menu import SECTION_COLLECTIONS
    from acorn.tui.settings_screen import (
        AddCollectionWizard,
        SettingsList,
        SettingsScreen,
        open_settings_section,
    )

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_COLLECTIONS)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        lst = screen.query_one(SettingsList)
        add_idx = next(i for i, it in enumerate(lst._items) if it.id == "collections.add")
        lst.cursor_index = add_idx
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, AddCollectionWizard)
        # All six field rows present.
        wlst = app.screen.query_one(SettingsList)
        labels = [it.label for it in wlst._items]
        for required in (
            "Name",
            "Source path",
            "Includes",
            "Excludes",
            "Frontmatter filter",
            "Follow symlinks",
        ):
            assert required in labels, f"missing field {required!r}; got {labels}"


@pytest.mark.asyncio
async def test_includes_field_opens_filetypes_picker(built_index: Path) -> None:
    """Spec: Wizard › Includes — multi-select of indexer-supported types."""
    from acorn.tui.settings_screen import (
        AddCollectionWizard,
        PickerScreen,
        SettingsList,
    )

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(AddCollectionWizard())
        await pilot.pause()
        wiz = app.screen
        assert isinstance(wiz, AddCollectionWizard)
        lst = wiz.query_one(SettingsList)
        # Move cursor to the Includes row.
        inc_idx = next(i for i, it in enumerate(lst._items) if it.id == "wiz.includes")
        lst.cursor_index = inc_idx
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PickerScreen)
        # The picker shows the indexer-supported types.
        from acorn.config import INDEXER_FILETYPES

        choice_values = [c.value for c in app.screen._choices]
        assert set(choice_values) == set(INDEXER_FILETYPES.keys())


@pytest.mark.asyncio
async def test_excludes_field_opens_presets_picker_with_defaults(built_index: Path) -> None:
    """Spec: Wizard › Excludes — preset multi-select, hidden pre-checked."""
    from acorn.tui.settings_screen import (
        AddCollectionWizard,
        PickerScreen,
        SettingsList,
    )

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(AddCollectionWizard())
        await pilot.pause()
        wiz = app.screen
        assert isinstance(wiz, AddCollectionWizard)
        lst = wiz.query_one(SettingsList)
        exc_idx = next(i for i, it in enumerate(lst._items) if it.id == "wiz.excludes")
        lst.cursor_index = exc_idx
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PickerScreen)
        # `hidden` preset is pre-selected.
        assert "hidden" in app.screen._selected


@pytest.mark.asyncio
async def test_path_validation_inline(tmp_path: Path, built_index: Path) -> None:
    """Spec: Wizard › Source path — live ✓/✗ inline validation."""
    from textual.widgets import Input, Static

    from acorn.tui.settings_screen import (
        AddCollectionWizard,
        EditBar,
        SettingsList,
    )

    real_dir = tmp_path / "exists"
    real_dir.mkdir()
    (real_dir / "a.md").write_text("hello")

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(AddCollectionWizard())
        await pilot.pause()
        wiz = app.screen
        assert isinstance(wiz, AddCollectionWizard)
        lst = wiz.query_one(SettingsList)
        path_idx = next(i for i, it in enumerate(lst._items) if it.id == "wiz.path")
        lst.cursor_index = path_idx
        await pilot.press("enter")
        await pilot.pause()
        bar = wiz.query_one(EditBar)
        # Type a path that does not exist.
        bar.query_one("#editor_input", Input).value = str(tmp_path / "nope")
        await pilot.pause()
        err = bar.query_one(".-edit-error", Static).render()
        assert "does not exist" in str(err).lower()
        # Type a path that does exist.
        bar.query_one("#editor_input", Input).value = str(real_dir)
        await pilot.pause()
        err = bar.query_one(".-edit-error", Static).render()
        assert "✓" in str(err) or "1 file" in str(err).lower()

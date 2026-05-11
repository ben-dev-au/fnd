"""Phase 3 (Settings UX redesign) — Add Collection wizard tests."""

from __future__ import annotations

from pathlib import Path

import pytest


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

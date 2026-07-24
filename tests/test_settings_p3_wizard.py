"""Phase 3 (Settings UX redesign) — Add Collection wizard tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.tui import FNDApp
from fnd.tui.indexer_service import IndexerService


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    from fnd.index import build_index

    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


def test_excludes_presets_exposed() -> None:
    """Spec: Wizard › Excludes — preset patterns, with safe defaults."""
    from fnd.config import EXCLUDES_PRESETS

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
    from fnd.tui import FNDApp
    from fnd.tui.menu import SECTION_COLLECTIONS
    from fnd.tui.settings_screen import (
        AddCollectionWizard,
        SettingsList,
        SettingsScreen,
        open_settings_section,
    )

    app = FNDApp(index_dir=built_index)
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
    """Wizard › Includes opens the nested ToggleTree picker, pre-selected to
    ALL types (a new source indexes every supported type by default)."""
    from fnd.kinds import ALL_KIND_IDS
    from fnd.tui.settings_screen import (
        AddCollectionWizard,
        SettingsList,
        TreePickerScreen,
    )
    from fnd.tui.widgets.toggle_tree import ToggleTree

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(AddCollectionWizard())
        await pilot.pause()
        wiz = app.screen
        assert isinstance(wiz, AddCollectionWizard)
        lst = wiz.query_one(SettingsList)
        inc_idx = next(i for i, it in enumerate(lst._items) if it.id == "wiz.includes")
        lst.cursor_index = inc_idx
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, TreePickerScreen)
        tree = app.screen.query_one("#tree_picker", ToggleTree)
        assert tree.selected == frozenset(ALL_KIND_IDS)


@pytest.mark.asyncio
async def test_excludes_field_opens_presets_picker_with_defaults(built_index: Path) -> None:
    """Spec: Wizard › Excludes — preset multi-select, hidden pre-checked."""
    from fnd.tui.settings_screen import (
        AddCollectionWizard,
        PickerScreen,
        SettingsList,
    )

    app = FNDApp(index_dir=built_index)
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

    from fnd.tui.settings_screen import (
        AddCollectionWizard,
        EditBar,
        SettingsList,
    )

    real_dir = tmp_path / "exists"
    real_dir.mkdir()
    (real_dir / "a.md").write_text("hello")

    app = FNDApp(index_dir=built_index)
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
        # Type a path that does not exist. Path validation is debounced
        # to avoid a per-keystroke disk walk, so wait past the timer.
        bar.query_one("#editor_input", Input).value = str(tmp_path / "nope")
        await pilot.pause(EditBar._PATH_VALIDATE_DEBOUNCE_S + 0.05)
        err = bar.query_one(".-edit-error", Static).render()
        assert "does not exist" in str(err).lower()
        # Type a path that does exist.
        bar.query_one("#editor_input", Input).value = str(real_dir)
        await pilot.pause(EditBar._PATH_VALIDATE_DEBOUNCE_S + 0.05)
        err = bar.query_one(".-edit-error", Static).render()
        assert "✓" in str(err) or "1 file" in str(err).lower()


@pytest.mark.asyncio
async def test_save_writes_collection_and_reindexes(
    tmp_path: Path, built_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: Wizard › Save — write_collection + reindex + drop on per-collection sub-screen."""
    from fnd.config import load
    from fnd.tui.settings_screen import (
        AddCollectionWizard,
        SettingsScreen,
    )

    # Redirect all config reads/writes to an isolated temp file.
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)

    real_dir = tmp_path / "vault"
    real_dir.mkdir()
    (real_dir / "a.md").write_text("# hello")

    # Wizard now routes the auto-reindex through _reindex_with_warning_if_needed,
    # which would push IndexerScreen on top. Stub it so we can assert on the
    # per-collection screen the wizard lands on.
    monkeypatch.setattr(
        IndexerService,
        "reindex_with_warning",
        lambda self, name, **kwargs: None,
    )

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        wiz = AddCollectionWizard()
        wiz._fields["name"] = "research"
        wiz._fields["path"] = str(real_dir)
        wiz._fields["includes"] = ["md"]
        wiz._fields["excludes_presets"] = ["hidden"]
        app.push_screen(wiz)
        await pilot.pause()
        # Trigger save.
        await pilot.press("ctrl+s")
        await pilot.pause()
        # We should land on the new collection's per-collection sub-screen.
        assert isinstance(app.screen, SettingsScreen)
        assert app.screen._breadcrumb == ("Collections", "research")
        # The on-disk config has the new collection with the right shape.
        cfg = load(cfg_path)
        assert "research" in cfg.collections
        src = cfg.collections["research"].sources[0]
        assert str(src.path) == str(real_dir)
        # Includes are mapped to globs.
        assert "**/*.md" in src.includes
        # Excludes from the `hidden` preset are present.
        assert any(".git" in g for g in src.excludes)


@pytest.mark.asyncio
async def test_esc_discards_wizard_with_no_side_effects(
    tmp_path: Path, built_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: Wizard › Esc — cancelling after typing a name does NOT
    create an empty collection."""
    from fnd.config import default_config_path, load
    from fnd.tui.settings_screen import AddCollectionWizard

    # Redirect all config reads/writes to an isolated temp file.
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)

    # Snapshot the (empty) config state before.
    cfg_path.write_text("")  # ensure file exists
    before = load(default_config_path()).collections.copy()

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        wiz = AddCollectionWizard()
        wiz._fields["name"] = "ghost"
        app.push_screen(wiz)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

    after = load(default_config_path()).collections
    assert "ghost" not in after, "Esc must not create an empty collection"
    assert set(after.keys()) == set(before.keys())


@pytest.mark.asyncio
async def test_includes_tree_picker_preserves_custom_glob(built_index: Path) -> None:
    """The nested Includes picker edits kinds only; an existing custom-glob
    include is preserved untouched when it commits."""
    from fnd.tui import FNDApp
    from fnd.tui.settings_screen import AddCollectionWizard

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        wiz = AddCollectionWizard()
        app.push_screen(wiz)
        await pilot.pause()
        wiz._fields["includes_custom"] = "**/*.org"
        wiz._set_includes(["md", "txt"])
        assert wiz._fields["includes"] == ["md", "txt"]
        assert wiz._fields["includes_custom"] == "**/*.org"


@pytest.mark.asyncio
async def test_excludes_picker_includes_custom_entry(built_index: Path) -> None:
    """Spec: Wizard › Excludes — `Custom glob…` escape hatch."""
    from fnd.tui import FNDApp
    from fnd.tui.settings_screen import (
        AddCollectionWizard,
        PickerScreen,
        SettingsList,
    )

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(AddCollectionWizard())
        await pilot.pause()
        wiz = app.screen
        assert isinstance(wiz, AddCollectionWizard)
        lst = wiz.query_one(SettingsList)
        idx = next(i for i, it in enumerate(lst._items) if it.id == "wiz.excludes")
        lst.cursor_index = idx
        await pilot.press("enter")
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, PickerScreen)
        values = [c.value for c in picker._choices]
        assert "__custom__" in values, f"expected `__custom__` choice; got {values}"


@pytest.mark.asyncio
async def test_set_includes_stores_kinds_all_maps_to_empty(built_index: Path) -> None:
    """_set_includes stores a selected-kind subset explicitly, and maps the
    all-selected case to an empty list (= index every supported type)."""
    from fnd.kinds import ALL_KIND_IDS
    from fnd.tui import FNDApp
    from fnd.tui.settings_screen import AddCollectionWizard

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        wiz = AddCollectionWizard()
        app.push_screen(wiz)
        await pilot.pause()
        wiz._set_includes(["md", "txt"])
        assert wiz._fields["includes"] == ["md", "txt"]
        wiz._set_includes(list(ALL_KIND_IDS))
        assert wiz._fields["includes"] == []


@pytest.mark.asyncio
async def test_source_form_uses_picker_for_includes(
    built_index: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: Per-source form — Includes is a multi-select picker pre-checked
    from the existing globs (parsed back into the indexer ext set)."""
    from fnd.config import (
        CollectionConfig,
        SourceConfig,
        write_collection,
    )
    from fnd.tui import FNDApp
    from fnd.tui.settings_screen import (
        SettingsList,
        SourceFormScreen,
        TreePickerScreen,
    )
    from fnd.tui.widgets.toggle_tree import ToggleTree

    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)

    real = tmp_path / "vault"
    real.mkdir()
    write_collection(
        config_path=cfg_path,
        name="probe2",
        collection=CollectionConfig(
            sources=[SourceConfig(path=real, includes=["**/*.md", "**/*.pdf"])]
        ),
    )

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        from fnd.config import load

        app._config = load()
        app.push_screen(SourceFormScreen(collection_name="probe2", source_index=0))
        await pilot.pause()
        form = app.screen
        assert isinstance(form, SourceFormScreen)
        lst = form.query_one(SettingsList)
        idx = next(i for i, it in enumerate(lst._items) if it.id == "form.includes")
        # The row must be a multi-select picker, not a free-text scalar.
        assert lst._items[idx].kind == "picker"
        lst.cursor_index = idx
        await pilot.press("enter")
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, TreePickerScreen)
        tree = picker.query_one("#tree_picker", ToggleTree)
        # md and pdf pre-selected from the existing globs.
        assert "md" in tree.selected
        assert "pdf" in tree.selected


@pytest.mark.asyncio
async def test_source_form_excludes_picker_round_trips_hidden_preset(
    built_index: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Excludes globs that match the `hidden` preset round-trip to a
    pre-selected `hidden` entry."""
    from fnd.config import (
        EXCLUDES_PRESETS,
        CollectionConfig,
        SourceConfig,
        write_collection,
    )
    from fnd.tui import FNDApp
    from fnd.tui.settings_screen import (
        PickerScreen,
        SettingsList,
        SourceFormScreen,
    )

    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)

    real = tmp_path / "vault"
    real.mkdir()
    write_collection(
        config_path=cfg_path,
        name="probe3",
        collection=CollectionConfig(
            sources=[
                SourceConfig(
                    path=real,
                    includes=["**/*.md"],
                    excludes=list(EXCLUDES_PRESETS["hidden"]["globs"]),
                )
            ]
        ),
    )

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        from fnd.config import load

        app._config = load()
        app.push_screen(SourceFormScreen(collection_name="probe3", source_index=0))
        await pilot.pause()
        form = app.screen
        assert isinstance(form, SourceFormScreen)
        lst = form.query_one(SettingsList)
        idx = next(i for i, it in enumerate(lst._items) if it.id == "form.excludes")
        lst.cursor_index = idx
        await pilot.press("enter")
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, PickerScreen)
        assert "hidden" in picker._selected


@pytest.mark.asyncio
async def test_save_with_missing_name_shows_inline_error(
    built_index: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: Locked decision #12 — inline error, no toast."""
    from textual.widgets import Static

    from fnd.tui.settings_screen import AddCollectionWizard

    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    real = tmp_path / "vault"
    real.mkdir()

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        wiz = AddCollectionWizard()
        # Path set, name blank.
        wiz._fields["path"] = str(real)
        app.push_screen(wiz)
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        # We should still be on the wizard.
        assert isinstance(app.screen, AddCollectionWizard)
        err = app.screen.query_one("#wizard_error", Static)
        rendered = str(err.render()).lower()
        assert "name" in rendered
        assert "required" in rendered
        # The widget is no longer hidden after the error fires.
        assert "-hidden" not in err.classes

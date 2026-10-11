"""Settings UX redesign: Add Collection wizard tests."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from fnd.config import Config

from fnd.tui import FNDApp
from fnd.tui.indexer_service import IndexerService
from tests._pilot_wait import screen_ready, settings_ready, wait_until


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    from fnd.index import build_index

    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


def test_excludes_presets_exposed() -> None:
    """The presets Excluded paths offers."""
    from fnd.config import EXCLUDES_PRESETS

    assert "hidden" in EXCLUDES_PRESETS
    hidden = EXCLUDES_PRESETS["hidden"]
    # Not the literal: the walk prunes hidden names whatever this preset says,
    # so the label may not offer to bring them back.
    assert "always" in str(hidden["label"]).lower(), hidden["label"]
    assert any(".git" in g for g in hidden["globs"])
    assert "node_modules" in EXCLUDES_PRESETS


@pytest.mark.asyncio
async def test_add_collection_pushes_wizard_with_expected_fields(built_index: Path) -> None:
    """Wizard rows, named as the rest of Settings names them.

    "Includes" picked file types here and path globs on the source form, from
    the same TOML key; each row also has to say what it does.
    """
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
        await settings_ready(pilot, app)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        lst = screen.query_one(SettingsList)
        add_idx = next(i for i, it in enumerate(lst._items) if it.id == "collections.add")
        lst.cursor_index = add_idx
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, AddCollectionWizard)
        # The collection's name, then its first source as the source form shows it.
        wlst = app.screen.query_one(SettingsList)
        from fnd.tui.menu import KIND_HEADER

        rows = [it for it in wlst._items if it.kind != KIND_HEADER]
        labels = [it.label for it in rows]
        assert labels == [
            "Name",
            "Path",
            "Follow symlinks",
            "Index filters",
            "Frontmatter rule",
            "Restrict to these paths",
        ], labels
        undescribed = [it.label for it in rows if not it.description]
        assert not undescribed, f"rows with nothing to explain them: {undescribed}"


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
        await screen_ready(pilot, app, AddCollectionWizard)
        wiz = app.screen
        assert isinstance(wiz, AddCollectionWizard)
        lst = wiz.query_one(SettingsList)
        path_idx = next(i for i, it in enumerate(lst._items) if it.id == "wiz.path")
        lst.cursor_index = path_idx
        await pilot.press("enter")
        await pilot.pause()
        bar = wiz.query_one(EditBar)

        def error_text() -> str:
            return str(bar.query_one(".-edit-error", Static).render())

        # Path validation is debounced to avoid a per-keystroke disk walk. The
        # debounce plus the walk plus the repaint is real work, and sleeping the
        # debounce is a wait for it only while the machine is idle — on Windows
        # the second read returned the FIRST path's verdict.
        bar.query_one("#editor_input", Input).value = str(tmp_path / "nope")
        await wait_until(
            pilot,
            lambda: "does not exist" in error_text().lower(),
            timeout=30.0,
            message="a missing path never reported itself missing",
        )
        # Type a path that does exist.
        bar.query_one("#editor_input", Input).value = str(real_dir)
        await wait_until(
            pilot,
            lambda: "✓" in error_text() or "1 file" in error_text().lower(),
            timeout=30.0,
            message="an existing path never validated",
        )


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
        wiz._fields["filters"] = {"kinds": ["md"], "excludes": ["**/.git/**"]}
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
        assert src.filters is not None
        assert "md" in (src.filters.kinds or [])
        # Excludes set in the wizard's Index filters are the source's own.
        assert src.filters.excludes == ["**/.git/**"]


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
async def test_source_form_shows_include_globs_as_ticked_file_types(
    built_index: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: existing include globs show as pre-checked file types.

    They are stated once, as ``filters.kinds``, so the guarantee lives
    in Index filters rather than a second picker beside it.
    """
    from textual.widgets import Static

    from fnd.config import (
        CollectionConfig,
        SourceConfig,
        write_collection,
    )
    from fnd.tui import FNDApp
    from fnd.tui.settings_screen import (
        FilterBrowserScreen,
        SettingsList,
        SourceFormScreen,
    )

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
        await screen_ready(pilot, app, SourceFormScreen)
        form = app.screen
        assert isinstance(form, SourceFormScreen)
        lst = form.query_one(SettingsList)
        assert not any(it.id == "form.includes" for it in lst._items), (
            "a second file-type picker beside Index filters is the duplication"
        )
        lst.cursor_index = next(i for i, it in enumerate(lst._items) if it.id == "form.filters")
        await pilot.press("right")
        for _ in range(400):
            await pilot.pause()
            if isinstance(app.screen, FilterBrowserScreen):
                break
        browser = app.screen
        assert isinstance(browser, FilterBrowserScreen)
        while browser._scanning:
            await pilot.pause()
        # The globs are not shown as ticked kinds: saving them back as kinds
        # would widen ``**/*.md`` to the whole ``md`` kind, ``.markdown``
        # included. The browser says they are in force instead.
        # Painted, not the stored renderable: the box refits to its width.
        box = browser.query_one("#filter_summary", Static)
        painted = " ".join(box.render_line(y).text for y in range(box.size.height))
        summary = " ".join(painted.split())
        assert "restricted to paths" in summary, summary
        assert "**/*.md" in summary


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


@pytest.mark.asyncio
async def test_the_wizard_editor_stays_inside_its_panel(built_index: Path) -> None:
    """It docked to the screen while the panel is centred, so it painted at
    the terminal's left edge, detached from the row it was editing."""
    from textual.widgets import Input

    from fnd.tui import FNDApp
    from fnd.tui.settings_screen import AddCollectionWizard, SettingsList

    app = FNDApp(index_dir=built_index)
    async with app.run_test(size=(94, 26)) as pilot:
        await pilot.pause()
        app.push_screen(AddCollectionWizard())
        for _ in range(20):
            await pilot.pause()
        wizard = app.screen
        panel = wizard.query_one("#settings_box")
        closed_width = panel.region.width
        rows = wizard.query_one(SettingsList)
        rows.cursor_index = next(i for i, it in enumerate(rows._items) if it.id == "wiz.name")
        await pilot.press("enter")
        for _ in range(8):
            await pilot.pause()
        editor = wizard.query_one("#editor_input", Input)
        assert editor.has_focus
        assert panel.region.contains_region(editor.region), "the editor left its panel"
        assert panel.region.width == closed_width, "the panel resized as editing began"


class TestTheWizardShowsWhatWillBeIndexed:
    """Setting nothing inherits `defaults.filters`, and the rows say so rather
    than implying no filter applies."""

    @staticmethod
    async def _rows(config: Config, pilot_size: tuple[int, int] = (110, 26)) -> dict[str, Any]:
        from fnd.tui import FNDApp
        from fnd.tui.settings_screen import AddCollectionWizard

        app = FNDApp(index_dir=Path("/nonexistent"), config=config)
        async with app.run_test(size=pilot_size) as pilot:
            await pilot.pause()
            app.push_screen(AddCollectionWizard())
            for _ in range(20):
                await pilot.pause()
            wizard = app.screen
            assert isinstance(wizard, AddCollectionWizard)
            return {
                item.id: item.value_getter(app)
                for item in wizard._build_field_items()
                if item.value_getter
            }

    @pytest.mark.asyncio
    async def test_inherited_filters_are_named(self) -> None:
        from fnd.config import Config, DefaultFilters, Defaults

        config = Config(
            defaults=Defaults(filters=DefaultFilters(kinds=["md"], frontmatter="Project == 'A'"))
        )
        rows = await self._rows(config)
        assert rows["wiz.filters"] == "inherited", rows["wiz.filters"]
        assert rows["wiz.frontmatter"] == "Project == 'A' (inherited)", rows["wiz.frontmatter"]

    @pytest.mark.asyncio
    async def test_without_defaults_it_still_reads_plainly(self) -> None:
        from fnd.config import Config

        rows = await self._rows(Config())
        assert rows["wiz.filters"] == "inherited"
        assert rows["wiz.frontmatter"] == "(none)"

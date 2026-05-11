"""Phase 3 (Settings UX redesign) — visual foundation tests."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_indexer_filetypes_exposed_and_complete() -> None:
    """Spec: Add Collection wizard › Includes — file types come from a
    single source of truth, not hardcoded in two places."""
    from acorn.config import INDEXER_FILETYPES

    # Map of extension -> human label. Order is the order the picker shows.
    assert tuple(INDEXER_FILETYPES) == ("md", "pdf", "docx", "pptx", "txt")
    assert INDEXER_FILETYPES["md"] == "Markdown (.md)"
    assert INDEXER_FILETYPES["pdf"] == "PDF (.pdf)"


def test_f3_no_longer_in_keymap() -> None:
    """Spec: Locked decisions — F3 dropped."""
    from acorn.tui.actions import load_keymap

    keymap = load_keymap()
    assert (
        "f3" not in keymap.bindings
    ), f"F3 should not be bound; keymap.bindings has: {keymap.bindings.get('f3')!r}"


def test_detail_strip_renders_description_and_metadata() -> None:
    """Spec: Visual system › Detail strip — 2 lines, description then
    metadata in $text-muted."""
    from acorn.tui.widgets.detail_strip import DetailStrip

    strip = DetailStrip()
    strip._description = "Result limit (1–1000) — max results returned per query."
    strip._metadata = "Stored in defaults.result_limit · Applies on next search"
    rendered = strip._render_lines()
    assert len(rendered) == 2
    assert "Result limit" in str(rendered[0])
    assert "Stored in defaults.result_limit" in str(rendered[1])


def test_row_with_key_renders_bracketed_accent() -> None:
    """Spec: Visual system › Key style — bracketed `[o]` accent."""
    from acorn.tui.menu import KIND_ACTION, MenuItem
    from acorn.tui.settings_screen import _render_row

    item = MenuItem(
        id="k.test",
        label="Open at locator",
        kind=KIND_ACTION,
        key="o",
        action_id="open_at_locator",
    )
    rendered = _render_row(item, app=None, width=80)
    text_str = str(rendered)
    assert "[o]" in text_str, f"expected '[o]' in rendered row; got: {text_str!r}"
    assert "▶" not in text_str


def test_root_container_hugs_content() -> None:
    """Spec: Visual system › Container — height: auto, not 1fr."""
    from acorn.tui.settings_screen import SettingsScreen

    css = SettingsScreen.CSS
    # Find the #settings_box rule and check its height.
    box_rule = css.split("#settings_box {")[1].split("}")[0]
    assert "height: auto" in box_rule
    assert "max-height" in box_rule
    assert "align: center middle" in css  # somewhere in the screen styles


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    from acorn.index import build_index

    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_root_rows_show_trailing_summaries(
    built_index: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: IA › Root — every drill row shows what's inside (always_show mode)."""
    from acorn.tui import AcornApp
    from acorn.tui.settings_screen import SettingsList, SettingsScreen

    # Isolate from the user's real config so drill_summary_mode stays at
    # the default "always_show" regardless of what is on disk.
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr("acorn.config.default_config_path", lambda: cfg_path)

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        lst = screen.query_one(SettingsList)
        by_label = {it.label: it for it in lst._items}
        preferences = by_label["Preferences"]
        assert preferences.trailing_value(app), "Preferences row needs a trailing summary"
        collections = by_label["Collections"]
        assert "collection" in collections.trailing_value(app).lower()
        keybindings = by_label["Keybindings"]
        assert "key" in keybindings.trailing_value(app).lower()


@pytest.mark.asyncio
async def test_collection_row_shows_source_count_and_ranking(
    built_index: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: IA › Collections sub-screen — each collection row's trailing
    shows source count and `ranking:<profile>` with scope dot prefix."""
    from acorn.config import (
        CollectionConfig,
        SourceConfig,
        write_collection,
    )
    from acorn.tui import AcornApp
    from acorn.tui.menu import SECTION_COLLECTIONS, section_items

    # Isolate config so we have a known "default" collection.
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr("acorn.config.default_config_path", lambda: cfg_path)

    real = tmp_path / "docs"
    real.mkdir()
    write_collection(
        config_path=cfg_path,
        name="default",
        collection=CollectionConfig(sources=[SourceConfig(path=real, includes=["**/*.md"])]),
    )

    app = AcornApp(index_dir=built_index)
    async with app.run_test():
        from acorn.config import load

        app._config = load()  # type: ignore[attr-defined]
        items = section_items(app, SECTION_COLLECTIONS)
        default = next(it for it in items if it.id == "collection.default")
        trailing = default.trailing_value(app)
        assert "source" in trailing.lower()
        assert "ranking" in trailing.lower()
        # Scope dot ● or ○ is rendered at the start.
        assert trailing[0] in ("●", "○")


@pytest.mark.asyncio
async def test_source_row_shows_filetypes_and_path_warning(
    tmp_path: Path, built_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: IA › Sources sub-screen — source rows show file-types and
    `⚠ path not found` when the path no longer resolves."""
    from acorn.config import (
        CollectionConfig,
        SourceConfig,
        write_collection,
    )
    from acorn.tui import AcornApp
    from acorn.tui.menu import _provider_sources

    # Isolate config writes.
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr("acorn.config.default_config_path", lambda: cfg_path)

    # Make a collection with two sources: one valid, one missing.
    real = tmp_path / "exists"
    real.mkdir()
    (real / "a.md").write_text("x")
    write_collection(
        config_path=cfg_path,
        name="probe",
        collection=CollectionConfig(
            sources=[
                SourceConfig(path=real, includes=["**/*.md"]),
                SourceConfig(path=tmp_path / "nope", includes=["**/*.pdf"]),
            ]
        ),
    )

    app = AcornApp(index_dir=built_index)
    async with app.run_test():
        # Reload config so the new collection is visible.
        from acorn.config import load

        app._config = load()  # type: ignore[attr-defined]
        items = _provider_sources(app, "probe")
        valid = next(it for it in items if it.id == "sources.probe.0")
        missing = next(it for it in items if it.id == "sources.probe.1")
        assert "md" in valid.trailing_value(app).lower()
        assert "⚠" in missing.trailing_value(app)


@pytest.mark.asyncio
async def test_detail_strip_updates_on_cursor_move(built_index: Path) -> None:
    """Spec: Visual system › Detail strip — populates on focus change."""
    from acorn.tui import AcornApp
    from acorn.tui.settings_screen import SettingsList, SettingsScreen
    from acorn.tui.widgets import DetailStrip

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        strip = screen.query_one(DetailStrip)
        # Cursor at index 0 (Preferences). Strip shows Preferences description.
        assert "Preferences" in strip._description or "preferences" in strip._description.lower()
        # Move cursor to Collections.
        lst = screen.query_one(SettingsList)
        lst.action_move(1)
        await pilot.pause()
        assert "Collections" in strip._description or "collection" in strip._description.lower()


@pytest.mark.asyncio
async def test_hint_bar_appends_reveal_when_cursor_on_reveal_capable_row(
    built_index: Path,
) -> None:
    """Spec: Hint bar — append `Shift+⏎ Reveal` when row supports reveal."""
    from acorn.tui import AcornApp
    from acorn.tui.settings_screen import SettingsList, SettingsScreen

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        lst = screen.query_one(SettingsList)
        idx = next(i for i, it in enumerate(lst._items) if it.id == "root.open_config_file")
        lst.cursor_index = idx
        await pilot.pause()
        # Assert on the resolved cluster (the actual logic) — Static.render
        # in the test pilot doesn't always reflect the last .update() call.
        cluster = screen._hint_cluster()
        labels = [label for _, label in cluster]
        assert (
            "Reveal" in labels
        ), f"expected Reveal hint on Open config row; got cluster: {cluster!r}"


@pytest.mark.asyncio
async def test_hint_bar_keybindings_variant(built_index: Path) -> None:
    """Spec: Hint bar — Keybindings screen shows `⏎ Run · [key] Run directly · Esc Back`."""
    from acorn.tui import AcornApp
    from acorn.tui.settings_screen import SettingsList, SettingsScreen

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_help()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        screen.query_one(SettingsList).focus()
        await pilot.pause()
        cluster = screen._hint_cluster()
        keys = [k for k, _ in cluster]
        labels = [lab for _, lab in cluster]
        assert "Run" in labels, f"expected Keybindings cluster; got: {cluster!r}"
        assert "[key]" in keys, f"expected press-key entry; got: {cluster!r}"


@pytest.mark.asyncio
async def test_hint_bar_search_focused_variant(built_index: Path) -> None:
    """Spec: Hint bar — Search input focused shows results / open-first / clear hints."""
    from textual.widgets import Input

    from acorn.tui import AcornApp
    from acorn.tui.settings_screen import SettingsScreen

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        screen.query_one("#settings_search", Input).focus()
        await pilot.pause()
        cluster = screen._hint_cluster()
        labels = [label for _, label in cluster]
        assert (
            "Clear" in labels or "Results" in labels
        ), f"expected search-focused cluster; got: {cluster!r}"


@pytest.mark.asyncio
async def test_hint_bar_edit_bar_open_variant(built_index: Path) -> None:
    """Spec: Hint bar — Edit-bar open shows `⏎ Save · Esc Cancel`."""
    from acorn.tui import AcornApp
    from acorn.tui.settings_screen import EditBar, SettingsScreen

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Open Preferences and drill into a scalar to open the EditBar.
        from acorn.tui.menu import SECTION_PREFERENCES
        from acorn.tui.settings_screen import (
            SettingsList,
            open_settings_section,
        )

        open_settings_section(app, SECTION_PREFERENCES)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        lst = screen.query_one(SettingsList)
        # Find a KIND_SCALAR row.
        from acorn.tui.menu import KIND_SCALAR

        idx = next(i for i, it in enumerate(lst._items) if it.kind == KIND_SCALAR)
        lst.cursor_index = idx
        screen._activate_item(lst._items[idx])
        await pilot.pause()
        # EditBar should be open (no -hidden class).
        bar = screen.query_one(EditBar)
        assert "-hidden" not in bar.classes
        cluster = screen._hint_cluster()
        labels = [label for _, label in cluster]
        assert "Save" in labels, f"expected Save in edit-bar cluster; got: {cluster!r}"
        assert "Cancel" in labels, f"expected Cancel in edit-bar cluster; got: {cluster!r}"


@pytest.mark.asyncio
async def test_root_screen_shows_version_status_line(built_index: Path) -> None:
    """Spec: Use case A4 — version visible at the bottom of the root menu."""
    from textual.widgets import Static

    from acorn import __version__
    from acorn.tui import AcornApp
    from acorn.tui.settings_screen import SettingsScreen

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        status = screen.query_one("#settings_status", Static)
        rendered = str(status.render())
        assert __version__ in rendered, f"expected version in status; got: {rendered!r}"


@pytest.mark.asyncio
async def test_subscreen_omits_version_status(built_index: Path) -> None:
    """Sub-screens don't carry the version line — only the root does."""
    from acorn.tui import AcornApp
    from acorn.tui.settings_screen import SettingsScreen

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_help()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        # Keybindings sub-screen → no version status.
        assert screen._breadcrumb == ("Keybindings",)
        from textual.css.query import NoMatches

        try:
            screen.query_one("#settings_status")
            raise AssertionError("subscreen should not mount #settings_status")
        except NoMatches:
            pass

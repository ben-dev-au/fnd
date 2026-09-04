"""Settings UX redesign — cross-section search tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.config import CollectionConfig, Config, SourceConfig
from fnd.index import build_index
from fnd.tui import FNDApp
from tests._pilot_wait import settings_ready


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


def _seed_config(fixtures_dir: Path) -> Config:
    """A Config with a single ``default`` collection so the Collections
    section walker yields a per-collection row labelled ``default``."""
    return Config(
        collections={
            "default": CollectionConfig(sources=[SourceConfig(path=fixtures_dir)]),
        }
    )


@pytest.mark.asyncio
async def test_walk_all_sections_includes_every_leaf(built_index: Path, fixtures_dir: Path) -> None:
    """Spec: Search behaviour › Index — walker covers Preferences,
    Collections, Keybindings, and root-level actions."""
    from fnd.tui.menu import KIND_HEADER, walk_all_sections

    app = FNDApp(index_dir=built_index, config=_seed_config(fixtures_dir))
    async with app.run_test():
        all_items = list(walk_all_sections(app))
        labels = {item.label for _path, item in all_items}
        # Preferences leaves:
        assert "Result limit" in labels
        assert "Default collection" in labels
        # Collections section includes the per-collection drill row.
        assert "default" in labels
        # Keybindings keys (sample). The provider derives from the
        # action registry — label is now the short title (Action
        # footer_label / command), description carries the long-form
        # explanation that surfaces in the DetailStrip.
        assert any(item.label == "Quit" for _, item in all_items)
        assert any("Quit fnd" in item.description for _, item in all_items)
        # Root action:
        assert "Config file" in labels
        # No headers leak through.
        assert not any(item.kind == KIND_HEADER for _, item in all_items)


@pytest.mark.asyncio
async def test_walk_includes_scope_pseudo_row(built_index: Path, fixtures_dir: Path) -> None:
    """Spec: Use cases › D — pre-empt confusion about active scope by
    surfacing a sidebar pointer in cross-section results."""
    from fnd.tui.menu import walk_all_sections

    app = FNDApp(index_dir=built_index, config=_seed_config(fixtures_dir))
    async with app.run_test():
        all_items = list(walk_all_sections(app))
        scope = next(
            (item for _, item in all_items if item.id == "pseudo.scope"),
            None,
        )
        assert scope is not None
        assert "sidebar" in scope.description.lower()
        # Keywords cover the obvious search terms.
        keywords = " ".join(scope.keywords).lower()
        assert "scope" in keywords
        assert "active" in keywords


@pytest.mark.asyncio
async def test_search_on_root_finds_preferences_leaf(built_index: Path, fixtures_dir: Path) -> None:
    """Spec: Search behaviour — typing on root surfaces leaves from
    every section, with the breadcrumb on each row."""
    from textual.widgets import Input

    from fnd.tui.settings_screen import SettingsList, SettingsScreen

    app = FNDApp(index_dir=built_index, config=_seed_config(fixtures_dir))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await settings_ready(pilot, app)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        search = screen.query_one("#settings_search", Input)
        search.value = "result limit"
        await pilot.pause()
        lst = screen.query_one(SettingsList)
        # The first item should be the Preferences › Result limit leaf.
        first = lst._items[0]
        assert first.label == "Result limit"


@pytest.mark.asyncio
async def test_search_on_keybindings_finds_preference(
    built_index: Path, fixtures_dir: Path
) -> None:
    """Spec: Cross-section search is global — searching from a sub-screen
    finds items in other sections."""
    from textual.widgets import Input

    from fnd.tui.settings_screen import SettingsList, SettingsScreen

    app = FNDApp(index_dir=built_index, config=_seed_config(fixtures_dir))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_help()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        search = screen.query_one("#settings_search", Input)
        search.value = "debounce"
        await pilot.pause()
        lst = screen.query_one(SettingsList)
        labels = [it.label for it in lst._items]
        assert any("Debounce" in label for label in labels)


@pytest.mark.asyncio
async def test_search_enter_navigates_only_then_second_enter_acts(
    built_index: Path, fixtures_dir: Path
) -> None:
    """Spec (revised): search is navigation-only. Enter in the search box
    lands focus on the first match and fires NO effect — no edit bar, no
    toggle, no drill, no side-effect. A second Enter on the now-focused
    list performs the action (here: opens the scalar's edit bar inline)."""
    from textual.widgets import Input

    from fnd.tui.settings_screen import (
        EditBar,
        SettingsList,
        SettingsScreen,
    )

    app = FNDApp(index_dir=built_index, config=_seed_config(fixtures_dir))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await settings_ready(pilot, app)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        search = screen.query_one("#settings_search", Input)
        search.value = "result limit"  # first match is the Result limit scalar
        await pilot.pause()
        lst = screen.query_one(SettingsList)
        bar = screen.query_one(EditBar)

        # First Enter (search focused) → navigate only.
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is screen, "must not drill away"
        assert app.focused is lst, "focus should move to the list"
        assert lst.cursor_index == 0, "cursor lands on the first match"
        assert "-hidden" in bar.classes, "edit bar must NOT auto-open on search Enter"

        # Second Enter (list focused) → acts: opens the edit bar inline.
        await pilot.press("enter")
        await pilot.pause()
        assert "-hidden" not in bar.classes


def test_search_result_label_has_bold_substring_for_query() -> None:
    """Spec: Search › Match display — matched substring rendered bold."""
    from fnd.tui.menu import KIND_SCALAR, MenuItem
    from fnd.tui.settings_screen import _render_row

    item = MenuItem(id="x", label="Result limit", kind=KIND_SCALAR)
    rendered = _render_row(item, app=None, width=80, highlight="result")
    label_str = str(rendered)
    assert "Result" in label_str
    # Walk the Rich Text spans and confirm at least one segment over the
    # "Result" substring carries a bold style.
    bold_segments = [s for s in rendered.spans if "bold" in str(s.style).lower()]
    assert bold_segments, "expected bold span over matched substring"


def test_render_row_no_highlight_when_query_misses() -> None:
    """Substring matching is case-insensitive but only bolds on a hit."""
    from fnd.tui.menu import KIND_SCALAR, MenuItem
    from fnd.tui.settings_screen import _render_row

    item = MenuItem(id="x", label="Result limit", kind=KIND_SCALAR)
    rendered = _render_row(item, app=None, width=80, highlight="zzz")
    label_str = str(rendered)
    assert "Result limit" in label_str
    # No bold span over the label range when there's no match.
    for span in rendered.spans:
        if "bold" not in str(span.style).lower():
            continue
        # The label "Result limit" lives at offsets [0, 12) since no key col.
        assert span.start >= len("Result limit"), (
            f"unexpected bold span over label area on no-match render: {span!r}"
        )


@pytest.mark.asyncio
async def test_zero_match_shows_empty_state_hint(built_index: Path) -> None:
    """Spec: Search › Empty-state hint — `No matches for '<q>'` placeholder."""
    from textual.widgets import Input

    from fnd.tui.settings_screen import SettingsList, SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await settings_ready(pilot, app)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        search = screen.query_one("#settings_search", Input)
        search.value = "zzzzzzz-no-match"
        await pilot.pause()
        lst = screen.query_one(SettingsList)
        labels = [it.label for it in lst._items]
        assert any("No matches" in label for label in labels), labels


def test_filter_haystack_excludes_description() -> None:
    """Spec: Search index covers label/key/keywords/breadcrumb — NOT
    description prose. Indexing descriptions muddies results."""
    import asyncio

    from fnd.config import CollectionConfig, Config, SourceConfig
    from fnd.tui import FNDApp
    from fnd.tui.settings_screen import SettingsScreen

    async def run() -> None:
        app = FNDApp()
        cfg = Config(collections={"x": CollectionConfig(sources=[SourceConfig(path=Path("."))])})
        app._config = cfg
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_open_command_palette()
            await settings_ready(pilot, app)
            screen = app.screen
            assert isinstance(screen, SettingsScreen)
            filtered, _bc = screen._filter_items("muddies")
            # "muddies" appears only in a description prose, never in label/keywords.
            assert filtered == [], (
                f"description prose leaked into the search index: {[m.label for m in filtered]}"
            )

    asyncio.run(run())

"""Phase 6: action registry, keymap loader, command palette."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.actions import (
    REGISTRY,
    load_keymap,
    resolve_command,
    validate_keymap,
)

# ── Registry sanity ─────────────────────────────────────────────────────


def test_registry_action_ids_unique() -> None:
    ids = [a.id for a in REGISTRY]
    assert len(ids) == len(set(ids)), f"duplicate ids in REGISTRY: {ids}"


def test_registry_palette_commands_unique() -> None:
    cmds = [a.palette_command for a in REGISTRY]
    assert len(cmds) == len(set(cmds)), f"duplicate command names: {cmds}"


def test_resolve_command_by_id_and_alias() -> None:
    a = resolve_command("focus_query")
    assert a is not None
    assert a.id == "focus_query"
    a = resolve_command("search")
    assert a is not None
    assert a.id == "focus_query"


def test_resolve_command_unknown_returns_none() -> None:
    assert resolve_command("nope") is None


# ── Keymap loader ──────────────────────────────────────────────────────


def test_default_keymap_includes_every_action_with_default_key() -> None:
    km = load_keymap(path=Path("/nonexistent"))
    for a in REGISTRY:
        if a.default_key is not None:
            # ``default_key`` may list several keys (comma-separated); each must
            # land in the keymap (see load_keymap — one action, multiple keys).
            for key in (k.strip() for k in a.default_key.split(",")):
                assert km.bindings.get(key) == a.id, f"action {a.id} default key {key!r} missing"


def test_user_overrides_replace_default(tmp_path: Path) -> None:
    cfg = tmp_path / "keybindings.toml"
    cfg.write_text(
        textwrap.dedent("""\
            [normal]
            "ctrl+x" = "open_default_app"
            "ctrl+f" = "focus_query"
        """),
        encoding="utf-8",
    )
    km = load_keymap(path=cfg)
    assert km.bindings["ctrl+x"] == "open_default_app"
    assert km.bindings["ctrl+f"] == "focus_query"
    # Defaults that weren't overridden should still be present.
    assert km.for_action("quit") == "q"


def test_unknown_action_in_user_keymap_is_dropped(tmp_path: Path) -> None:
    cfg = tmp_path / "keybindings.toml"
    cfg.write_text(
        textwrap.dedent("""\
            [normal]
            "ctrl+x" = "fly_to_the_moon"
        """),
        encoding="utf-8",
    )
    km = load_keymap(path=cfg)
    assert "ctrl+x" not in km.bindings
    # And validate surfaces it as a warning.
    warnings = validate_keymap(path=cfg)
    assert any("fly_to_the_moon" in w for w in warnings)


# ── Settings menu (replaces the old command palette / help overlay) ───


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_open_command_palette_pushes_settings_menu(built_index: Path) -> None:
    """`:` opens the unified Settings & Commands menu (replaces the old
    one-shot palette input)."""
    from fnd.tui.settings_screen import SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        # Reachable root — the menu's top level shows every section.
        assert app.screen._breadcrumb == ()
        # Second press closes the stack.
        app.action_open_command_palette()
        await pilot.pause()
        assert not isinstance(app.screen, SettingsScreen)


@pytest.mark.asyncio
async def test_root_menu_search_is_cross_section(built_index: Path) -> None:
    """The root menu's search Input walks every section (cross-section
    search). Typing a term that matches a leaf in the Keybindings section
    surfaces those keybinding rows, not just the top-level category row."""
    from textual.widgets import Input

    from fnd.tui.menu import KIND_HEADER
    from fnd.tui.settings_screen import SettingsList, SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        search = screen.query_one("#settings_search", Input)
        search.value = "keybindings"
        await pilot.pause()
        lst = screen.query_one(SettingsList)
        # Cross-section search surfaces leaves from the Keybindings section
        # (each row's breadcrumb is "Keybindings"). The word "keybindings"
        # matches via the breadcrumb segment, so we get individual key rows.
        # It also surfaces the root "Keybindings file" action
        # (breadcrumb is ()) because "keybindings" appears in its keywords.
        selectable = [item for item in lst._items if item.kind != KIND_HEADER]
        assert len(selectable) > 0
        # Every result has a breadcrumb pointing either to Keybindings section
        # rows or to the root-level open-keybindings-file action.
        valid_breadcrumbs = {("Keybindings",), ()}
        assert all(
            screen._search_breadcrumbs.get(id(item)) in valid_breadcrumbs for item in selectable
        )


@pytest.mark.asyncio
async def test_show_help_pushes_keybindings_subscreen(built_index: Path) -> None:
    """`?` pushes the Keybindings sub-screen directly — one Esc returns
    to the main app."""
    from fnd.tui.settings_screen import SettingsList, SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_help()
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        assert app.screen._breadcrumb == ("Keybindings",)
        # Every REGISTRY action with a bound key shows up in the list.
        # The provider is now registry-derived (single source of truth).
        # Labels are short titles (footer_label / command); the long
        # form lives on description so the DetailStrip adds value.
        lst = app.screen.query_one(SettingsList)
        labels = " ".join(item.label for item in lst._items)
        descriptions = " ".join(item.description for item in lst._items)
        assert "Search" in labels  # focus_query → footer_label "Search"
        assert "Quit" in labels  # quit → footer_label "Quit"
        assert "Focus the query input" in descriptions  # focus_query description
        assert "Quit fnd" in descriptions  # quit description

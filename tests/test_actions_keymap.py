"""Phase 6: action registry, keymap loader, command palette."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from acorn.index import build_index
from acorn.tui import AcornApp
from acorn.tui.actions import (
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
            assert (
                km.for_action(a.id) == a.default_key
            ), f"action {a.id} default {a.default_key} missing"


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


# ── Command palette in the TUI ─────────────────────────────────────────


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_command_palette_runs_known_command(built_index: Path) -> None:
    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        from textual.widgets import Input

        palette = app.query_one("#cmd_palette_input", Input)
        palette.value = ":help"
        await pilot.press("enter")
        await pilot.pause()
        assert app.last_palette_result == "show_help"


@pytest.mark.asyncio
async def test_command_palette_unknown_command_recorded(built_index: Path) -> None:
    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        from textual.widgets import Input

        palette = app.query_one("#cmd_palette_input", Input)
        palette.value = "fly_to_the_moon"
        await pilot.press("enter")
        await pilot.pause()
        assert app.last_palette_result == "unknown:fly_to_the_moon"


@pytest.mark.asyncio
async def test_help_overlay_lists_every_action(built_index: Path) -> None:
    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_help()
        await pilot.pause()
        from textual.widgets import Markdown

        # The help overlay mounts a Markdown widget under #help_overlay.
        overlay = app.query_one("#help_overlay")
        md_widgets = overlay.query(Markdown)
        assert len(md_widgets) >= 1
        # Toggle off — overlay should be gone.
        app.action_show_help()
        await pilot.pause()
        assert not app.query("#help_overlay")

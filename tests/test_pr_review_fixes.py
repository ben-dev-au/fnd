"""Regression tests for the PR #4 review fixes.

Each test pins one of the gaps reviewers flagged so they can't drift
back without breaking a guard:

* (#1) Obsidian ``file_in_vault`` is computed relative to the actual
  vault root, not the source root — fixes deep-link breakage when a
  source is configured as a subdirectory of the vault.
* (#4) KIND_ACTION rows with empty ``action_id`` (widget-only doc
  rows in the keybindings cheat sheet) don't dismiss the settings
  stack on Enter.
* (#5) The per-source App picker filters by ``app.available()``,
  matching the Preferences picker and the Open-with modal.
* (#6) ``_keybindings_context_hint`` uses ``isinstance`` against
  SourceFormScreen, not a string match on the class name.
* (#7) Keybinding row ids are unique even when labels collide across
  sections (multiple "Cancel" rows used to all collapse to
  ``key.cancel``).
* (#3) User-defined ``[apps.<id>]`` templates with unknown
  placeholders are rejected at load time, not at open time.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from fnd import apps
from fnd.apps import (
    BUILTIN_APPS,
    OpenRequest,
    detect_obsidian_vault,
    detect_obsidian_vault_path,
    load_user_apps,
)

# ── #1 — vault-relative file_in_vault ──────────────────────────────────────


def test_detect_obsidian_vault_path_returns_root_path(tmp_path: Path) -> None:
    """Sibling helper to ``detect_obsidian_vault`` (which only returns
    the basename). Returns the absolute path to the directory containing
    ``.obsidian/``, walking up from any depth."""
    vault = tmp_path / "MyVault"
    (vault / ".obsidian").mkdir(parents=True)
    nested = vault / "Notes" / "Course"
    nested.mkdir(parents=True)
    note = nested / "lecture.md"
    note.write_text("# x")
    assert detect_obsidian_vault_path(note) == vault
    # Name helper still works for the picker UI.
    assert detect_obsidian_vault(note) == "MyVault"


def test_detect_obsidian_vault_path_none_when_no_vault(tmp_path: Path) -> None:
    note = tmp_path / "loose" / "note.md"
    note.parent.mkdir()
    note.write_text("# x")
    assert detect_obsidian_vault_path(note) is None


def test_open_smart_computes_file_in_vault_relative_to_vault_not_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The PR #4 bug: when source.path is a subdirectory of the
    vault (e.g. an Obsidian collection that indexes only one
    course's notes folder), ``file_in_vault`` was computed relative
    to source.path — Advanced URI would then look for the file at
    the wrong location in the vault."""
    from types import SimpleNamespace

    from fnd import opener

    vault = tmp_path / "MyVault"
    (vault / ".obsidian").mkdir(parents=True)
    course_dir = vault / "Notes" / "Algorithms"
    course_dir.mkdir(parents=True)
    note = course_dir / "ch1.md"
    note.write_text("# x")

    source = SimpleNamespace(
        path=course_dir,
        app="obsidian",
        app_for={},
        app_params={"vault": "MyVault"},
    )

    captured: list[OpenRequest] = []

    def fake_handler(req: OpenRequest) -> int:
        captured.append(req)
        return 0

    # Stub the obsidian handler to capture the OpenRequest that
    # ``open_smart`` builds — we're testing OpenRequest assembly here,
    # not URL rendering.
    monkeypatch.setitem(
        BUILTIN_APPS,
        "obsidian",
        BUILTIN_APPS["obsidian"].__class__(
            id=BUILTIN_APPS["obsidian"].id,
            display_name=BUILTIN_APPS["obsidian"].display_name,
            handles=BUILTIN_APPS["obsidian"].handles,
            handler=fake_handler,
            available=BUILTIN_APPS["obsidian"].available,
            positional=BUILTIN_APPS["obsidian"].positional,
            notes=BUILTIN_APPS["obsidian"].notes,
        ),
    )
    monkeypatch.setattr(opener, "_has_skim", lambda: False)
    # Isolate config load so the test doesn't read the developer's real
    # ~/Library/Application Support/fnd/config.toml.
    monkeypatch.setattr("fnd.config.load", lambda: None)

    opener.open_smart(
        path=note,
        kind="md",
        source=source,
        query="x",
    )
    assert captured, "obsidian handler was not invoked"
    req = captured[0]
    # MUST be relative to vault root, not source root.
    assert req.file_in_vault == "Notes/Algorithms/ch1.md", req.file_in_vault


# ── #4 — KIND_ACTION + empty action_id is a no-op ──────────────────────────


@pytest.mark.asyncio
async def test_enter_on_widget_only_row_does_not_close_settings_stack() -> None:
    """Pressing Enter on a documentation-only row (e.g. 'Move cursor'
    in the Settings menu section of the Keybindings cheat sheet) is
    a no-op. Previously every KIND_ACTION row called
    ``_close_settings_stack()`` regardless of whether action_id was
    set, so the user dismissing the help got dropped to the main app."""
    from fnd.tui import FNDApp
    from fnd.tui.menu import SECTION_KEYBINDINGS
    from fnd.tui.settings_screen import (
        SettingsList,
        SettingsScreen,
        open_settings_section,
    )

    app = FNDApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_KEYBINDINGS)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        lst = screen.query_one(SettingsList)
        # Cursor onto a widget-only row.
        idx = next(
            i for i, it in enumerate(lst._items) if it.label == "Move cursor" and not it.action_id
        )
        lst.cursor_index = idx
        lst.focus()
        await pilot.press("enter")
        await pilot.pause()
        # Stack must still have Keybindings on top — user is reading
        # help, hitting Enter on a doc row shouldn't dismiss anything.
        assert isinstance(app.screen, SettingsScreen)
        assert app.screen._breadcrumb == ("Keybindings",)


# ── #5 — per-source App picker filters unavailable ────────────────────────


@pytest.mark.asyncio
async def test_per_source_app_picker_filters_unavailable_apps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Make Skim 'unavailable' and confirm the per-source App picker
    doesn't list it (matches the Preferences picker behaviour). System
    stays — it's always available."""
    from fnd.config import CollectionConfig, Config, SourceConfig
    from fnd.tui import FNDApp
    from fnd.tui.settings_screen import SourceFormScreen

    monkeypatch.setattr(apps, "_skim_app_exists", lambda: False)

    cfg = Config(collections={"x": CollectionConfig(sources=[SourceConfig(path=tmp_path)])})
    app = FNDApp(config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        form = SourceFormScreen(collection_name="x", source_index=0)
        app.push_screen(form)
        await pilot.pause()
        choices = form._app_choices(None)
        ids = [c.value for c in choices]
        assert "skim" not in ids, ids
        assert "system" in ids


# ── #6 — isinstance check for SourceFormScreen ────────────────────────────


def test_keybindings_context_hint_uses_isinstance_not_string_check() -> None:
    """Static inspection of the helper: it must reference the
    SourceFormScreen class via isinstance, not a brittle substring
    match on the class name."""
    import inspect

    from fnd.tui.app import FNDApp

    src = inspect.getsource(FNDApp._keybindings_context_hint)
    assert "isinstance(current, SourceFormScreen)" in src, (
        "context hint must use isinstance, not name-string check"
    )
    assert '"SourceFormScreen" in type(' not in src


# ── #7 — keybinding row ids are unique ────────────────────────────────────


def test_keybinding_row_ids_are_unique() -> None:
    """The Source form's 'Cancel' and the Open-with modal's 'Cancel'
    used to both collapse to ``key.cancel``. With section in the slug,
    every widget-only row gets a distinct id."""
    from types import SimpleNamespace
    from typing import cast

    from fnd.tui import FNDApp
    from fnd.tui.menu import KIND_HEADER, _provider_keybindings

    items = _provider_keybindings(cast(FNDApp, SimpleNamespace()))
    ids = [it.id for it in items if it.kind != KIND_HEADER]
    counts = Counter(ids)
    dupes = [k for k, v in counts.items() if v > 1]
    assert not dupes, dupes


# ── #3 — user template validation at load ─────────────────────────────────


def test_load_user_apps_rejects_unknown_placeholder_in_argv() -> None:
    """A typo'd placeholder in an argv template (e.g. {ptha}) must
    surface at config load as a clean ValueError naming the bad
    placeholder — not at open time as an opaque KeyError."""
    cfg = {
        "myapp": {
            "display_name": "MyApp",
            "handles": ["md"],
            "argv": ["myapp", "-f", "{ptha}"],  # typo: {ptha} not {path}
        }
    }
    with pytest.raises(ValueError, match=r"ptha"):
        load_user_apps(cfg)


def test_load_user_apps_rejects_unknown_placeholder_in_url() -> None:
    cfg = {
        "myapp": {
            "display_name": "MyApp",
            "handles": ["md"],
            "url": "myapp://{patg_pct}",  # typo: {patg_pct} not {path_pct}
        }
    }
    with pytest.raises(ValueError, match=r"patg_pct"):
        load_user_apps(cfg)


def test_load_user_apps_accepts_all_documented_placeholders() -> None:
    """Sanity check: every variable listed in docs/apps/README.md
    must render without KeyError. Catches the case where the docs
    drift away from ``_render_vars`` or vice versa."""
    placeholders = [
        "{path}",
        "{path_pct}",
        "{page}",
        "{slide}",
        "{line}",
        "{heading}",
        "{heading_pct}",
        "{query}",
        "{query_pct}",
        "{vault}",
        "{vault_pct}",
        "{file_in_vault}",
        "{file_in_vault_pct}",
    ]
    cfg = {
        "myapp": {
            "display_name": "MyApp",
            "handles": ["md"],
            "argv": ["myapp", *placeholders],
        }
    }
    # No exception → all placeholders resolve in the stub OpenRequest.
    load_user_apps(cfg)

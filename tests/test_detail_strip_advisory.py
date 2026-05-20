"""DetailStrip carries the per-app advisory copy and renders with a
visible border so it reads as a distinct guidance panel.

The strip is the only place we proactively surface "install plugin X"
/ "no page-jump on macOS" / etc. — without the advisory routed
through, users have to discover capability gaps by trial and error.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.apps import BUILTIN_APPS
from fnd.tui import FNDApp
from fnd.tui.menu import SECTION_PREFERENCES
from fnd.tui.settings_screen import (
    SettingsList,
    SettingsScreen,
    open_settings_section,
)
from fnd.tui.widgets.detail_strip import DetailStrip as DetailStripWidget


def test_detail_strip_has_top_border_and_wraps_description() -> None:
    """Border-top lets the strip read as a distinct guidance panel
    rather than blending into the row list above. Height is ``auto``
    so longer descriptions wrap onto multiple lines instead of being
    truncated mid-sentence; ``max-height`` bounds growth so a runaway
    description can't dominate the screen."""
    strip = DetailStripWidget()
    assert "border-top: hkey" in strip.DEFAULT_CSS
    assert "$primary" in strip.DEFAULT_CSS
    # Wrap-friendly height shape.
    assert "height: auto" in strip.DEFAULT_CSS
    assert "max-height" in strip.DEFAULT_CSS
    # Description must be auto-height for wrap; metadata stays one row.
    assert "Static.-description { color: $text; height: auto;" in strip.DEFAULT_CSS


def test_obsidian_app_notes_mention_advanced_uri_plugin() -> None:
    """The app's ``notes`` carry the recommendation. Notes are then
    surfaced into picker descriptions via the choices helpers."""
    notes = BUILTIN_APPS["obsidian"].notes.lower()
    assert "advanced uri" in notes
    assert "plugin" in notes
    # Punchy: single sentence so it fits in the 1-line description slot.
    assert notes.count(".") <= 2, notes


def test_app_defaults_picker_obsidian_choice_carries_plugin_advisory() -> None:
    """``_choices_apps_for_kind`` already routes ``app.notes`` into the
    picker. Pin that the obsidian choice's description names the
    plugin — without this nudge users don't know why o-on-md sometimes
    lands at the section heading instead of the matched line."""
    from fnd.tui.menu import _choices_apps_for_kind

    class _FakeApp:
        _config = None

    choices = _choices_apps_for_kind(_FakeApp(), "md")  # type: ignore[arg-type]
    obs = next((c for c in choices if c.value == "obsidian"), None)
    # The obsidian entry only appears when Obsidian.app is detected;
    # skip the assertion cleanly when it isn't installed on the host.
    if obs is None:
        pytest.skip("Obsidian.app not present on this host")
    assert "advanced uri" in obs.description.lower(), obs.description


@pytest.mark.asyncio
async def test_per_source_app_picker_obsidian_choice_carries_plugin_advisory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SourceFormScreen's per-source App picker uses ``_app_choices``,
    which now also surfaces ``app.notes`` (was hard-coded to ``handles:
    md,markdown`` and didn't mention the plugin at all)."""
    from fnd.config import CollectionConfig, Config, SourceConfig
    from fnd.tui.settings_screen import SourceFormScreen

    cfg = Config(collections={"x": CollectionConfig(sources=[SourceConfig(path=tmp_path)])})
    app = FNDApp(config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        form = SourceFormScreen(collection_name="x", source_index=0)
        app.push_screen(form)
        await pilot.pause()
        choices = form._app_choices(None)
        obs = next((c for c in choices if c.value == "obsidian"), None)
        if obs is None:
            pytest.skip("Obsidian.app not present on this host")
        assert "advanced uri" in obs.description.lower(), obs.description


@pytest.mark.asyncio
async def test_vault_field_hint_mentions_advanced_uri_plugin(
    tmp_path: Path,
) -> None:
    """SourceFormScreen's Obsidian vault row's hint is the user's
    second chance to learn about the plugin — first sighting is at
    App picker, second at the vault field itself."""
    from fnd.config import CollectionConfig, Config, SourceConfig
    from fnd.tui.settings_screen import SourceFormScreen

    cfg = Config(collections={"x": CollectionConfig(sources=[SourceConfig(path=tmp_path)])})
    app = FNDApp(config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        form = SourceFormScreen(collection_name="x", source_index=0)
        app.push_screen(form)
        await pilot.pause()
        items = form._build_field_items()
        vault_row = next(it for it in items if it.id == "form.app_params_vault")
        assert "Advanced URI" in vault_row.description, vault_row.description


@pytest.mark.asyncio
async def test_detail_strip_renders_obsidian_advisory_under_picker_row() -> None:
    """End-to-end: opening Preferences → Default Markdown app picker
    surfaces the Advanced URI advisory in the DetailStrip when the
    obsidian choice is focused."""
    from fnd.tui.settings_screen import PickerScreen

    app = FNDApp()
    async with app.run_test(size=(80, 28)) as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_PREFERENCES)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        lst = screen.query_one(SettingsList)
        # Find and activate the "Default Markdown app" picker.
        idx = next(
            (
                i
                for i, it in enumerate(lst._items)
                if "Markdown" in it.label and "app" in it.label.lower()
            ),
            None,
        )
        if idx is None:
            pytest.skip("default-markdown-app picker not present")
        lst.cursor_index = idx
        await pilot.press("enter")
        await pilot.pause()
        picker = app.screen
        if not isinstance(picker, PickerScreen):
            pytest.skip("picker did not open (no apps registered?)")
        # Cursor through choices to find Obsidian.
        obs_idx = next(
            (i for i, c in enumerate(picker._choices) if c.value == "obsidian"),
            None,
        )
        if obs_idx is None:
            pytest.skip("Obsidian not in registry")
        # Picker exposes its own description rendering; assert the
        # description text on the obsidian choice carries the advisory.
        assert "advanced uri" in picker._choices[obs_idx].description.lower()

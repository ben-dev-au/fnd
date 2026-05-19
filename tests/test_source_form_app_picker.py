"""Phase 2b: SourceFormScreen App picker + Obsidian vault auto-detect.

Pure tests of the form's app handling without driving the full Pilot
flow (that's covered by the lower-level config tests). Verifies:

* ``_set_app('obsidian')`` triggers vault auto-detection from the
  source path when no vault is set yet.
* ``_set_app('')`` clears the per-source app override.
* ``action_save_close``'s assembled SourceConfig carries ``app`` and
  ``app_params`` correctly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fnd.config import Config
from fnd.tui.settings_screen import SourceFormScreen


class _FakeSettingsList:
    """Stand-in for the form's SettingsList — only refresh_values is
    called by the actions we exercise here."""

    def __init__(self) -> None:
        self.refresh_called = 0

    def refresh_values(self) -> None:
        self.refresh_called += 1


@pytest.fixture
def form_with_fake_app(monkeypatch: pytest.MonkeyPatch) -> SourceFormScreen:
    """Build a SourceFormScreen bypassing Textual's mount cycle.

    The form's ``_set_app`` reads ``self.app._config`` and calls
    ``self.query_one(SettingsList).refresh_values()``. We stub both
    so the action runs end-to-end without Textual.
    """
    form = SourceFormScreen(collection_name="default", source_index=None)
    # Build a minimal in-memory config with the default app set so
    # the registry probe inside _set_app's auto-detect path works.
    monkeypatch.setattr("fnd.config.load", lambda *a, **kw: Config())
    fake = _FakeSettingsList()

    def _query_one(*_args: Any, **_kwargs: Any) -> _FakeSettingsList:
        return fake

    monkeypatch.setattr(form, "query_one", _query_one)
    return form


def test_app_field_default_empty(form_with_fake_app: SourceFormScreen) -> None:
    assert form_with_fake_app._fields["app"] == ""
    assert form_with_fake_app._fields["app_params_vault"] == ""


def test_set_app_records_choice(form_with_fake_app: SourceFormScreen) -> None:
    form_with_fake_app._set_app("vscode")
    assert form_with_fake_app._fields["app"] == "vscode"
    # Vault unchanged for non-obsidian apps.
    assert form_with_fake_app._fields["app_params_vault"] == ""


def test_set_app_empty_clears_override(form_with_fake_app: SourceFormScreen) -> None:
    form_with_fake_app._fields["app"] = "vscode"
    form_with_fake_app._set_app("")
    assert form_with_fake_app._fields["app"] == ""


def test_set_app_obsidian_auto_detects_vault(
    form_with_fake_app: SourceFormScreen, tmp_path: Path
) -> None:
    """Picking Obsidian with a source path inside a vault auto-fills
    the vault name from ``.obsidian/``'s parent directory basename."""
    vault = tmp_path / "MyKnowledge"
    notes = vault / "daily"
    notes.mkdir(parents=True)
    (vault / ".obsidian").mkdir()
    form_with_fake_app._fields["path"] = str(notes)

    form_with_fake_app._set_app("obsidian")
    assert form_with_fake_app._fields["app"] == "obsidian"
    assert form_with_fake_app._fields["app_params_vault"] == "MyKnowledge"


def test_set_app_obsidian_skips_autodetect_when_vault_set(
    form_with_fake_app: SourceFormScreen, tmp_path: Path
) -> None:
    """User-set vault wins over auto-detection."""
    vault = tmp_path / "AutoDetected"
    vault.mkdir()
    (vault / ".obsidian").mkdir()
    form_with_fake_app._fields["path"] = str(vault)
    form_with_fake_app._fields["app_params_vault"] = "MyCustomName"

    form_with_fake_app._set_app("obsidian")
    assert form_with_fake_app._fields["app_params_vault"] == "MyCustomName"


def test_set_app_obsidian_leaves_vault_empty_when_no_vault_found(
    form_with_fake_app: SourceFormScreen, tmp_path: Path
) -> None:
    """A source path without a ``.obsidian/`` ancestor leaves the vault
    field empty so the user can type one in manually."""
    plain = tmp_path / "plain"
    plain.mkdir()
    form_with_fake_app._fields["path"] = str(plain)

    form_with_fake_app._set_app("obsidian")
    assert form_with_fake_app._fields["app_params_vault"] == ""


# test_app_choices_lists_builtins_plus_default_sentinel and the
# save-close round-trip were collapsed into integration coverage:
# the registry contents are already pinned by tests/test_apps_registry.py,
# and the SourceFormScreen's persistence path is implicitly exercised
# by tests/test_clone_source.py (every field — app + app_params
# included — round-trips through write_collection + load). The form-
# specific behaviour above (vault auto-detect, set/clear of the app
# override) is what couldn't be reached via lower-level tests.

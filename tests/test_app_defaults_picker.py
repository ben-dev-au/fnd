"""Preferences-level per-filetype default-app picker.

Pure tests of the new ``pref.app_defaults.<kind>`` menu rows in
``_provider_preferences``. The picker writes to ``[app_defaults]``
in the user config and the resolver picks it up on the next open.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd import apps
from fnd.config import Config, load, write_setting


def test_picker_lists_apps_that_handle_the_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """For PDF, the picker should list every available app whose
    ``handles`` covers 'pdf' (skim, preview, pdf_expert, system) plus
    the '(auto-resolve)' sentinel — and NOT obsidian/vscode (md only)."""
    from fnd.tui.menu import _choices_apps_for_kind

    monkeypatch.setattr(apps, "_skim_app_exists", lambda: True)
    monkeypatch.setattr(apps, "_preview_app_exists", lambda: True)
    monkeypatch.setattr(apps, "_pdf_expert_app_exists", lambda: True)
    monkeypatch.setattr(apps, "_obsidian_app_exists", lambda: True)
    monkeypatch.setattr(apps, "_vscode_cli_exists", lambda: True)

    class FakeApp:
        _config = Config()

    choices = _choices_apps_for_kind(FakeApp(), "pdf")  # type: ignore[arg-type]
    ids = [c.value for c in choices]
    assert ids[0] == ""  # auto-resolve sentinel always first
    for needed in ("system", "skim", "preview", "pdf_expert"):
        assert needed in ids, f"{needed} missing from PDF choices: {ids}"
    # md-only apps must NOT appear in the PDF picker.
    assert "obsidian" not in ids
    # vscode has wildcard '*' handle so it DOES appear (it can open
    # PDF as text — user choice).
    assert "vscode" in ids


def test_picker_excludes_unavailable_apps(monkeypatch: pytest.MonkeyPatch) -> None:
    from fnd.tui.menu import _choices_apps_for_kind

    monkeypatch.setattr(apps, "_skim_app_exists", lambda: False)  # not installed
    monkeypatch.setattr(apps, "_preview_app_exists", lambda: True)
    monkeypatch.setattr(apps, "_pdf_expert_app_exists", lambda: False)
    monkeypatch.setattr(apps, "_vscode_cli_exists", lambda: False)

    class FakeApp:
        _config = Config()

    choices = _choices_apps_for_kind(FakeApp(), "pdf")  # type: ignore[arg-type]
    ids = [c.value for c in choices]
    assert "skim" not in ids
    assert "pdf_expert" not in ids
    assert "vscode" not in ids
    assert "preview" in ids
    assert "system" in ids


def test_setter_writes_value_to_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Picking an app writes ``[app_defaults] <kind> = <id>`` to config."""
    from fnd import config as cfg_mod
    from fnd.tui.menu import _set_app_default_for_kind

    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr(cfg_mod, "default_config_path", lambda: cfg_path)

    class FakeApp:
        _config = Config()

        def _refresh_status(self) -> None:
            pass

    app = FakeApp()
    _set_app_default_for_kind(app, "pdf", "preview")  # type: ignore[arg-type]

    persisted = load(cfg_path)
    assert persisted.app_defaults.get("pdf") == "preview"


def test_setter_with_empty_value_clears_existing_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Picking the '(auto-resolve)' sentinel removes the explicit
    ``[app_defaults] pdf`` entry so the resolver's auto-promote ladder
    takes over."""
    from fnd import config as cfg_mod
    from fnd.tui.menu import _set_app_default_for_kind

    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr(cfg_mod, "default_config_path", lambda: cfg_path)

    # Seed with an explicit default already set.
    write_setting(config_path=cfg_path, dotted_path="app_defaults.pdf", value="preview")
    seeded = load(cfg_path)
    assert seeded.app_defaults["pdf"] == "preview"

    class FakeApp:
        _config = seeded

        def _refresh_status(self) -> None:
            pass

    _set_app_default_for_kind(FakeApp(), "pdf", "")  # type: ignore[arg-type]

    persisted = load(cfg_path)
    assert "pdf" not in persisted.app_defaults


def test_getter_returns_current_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from fnd.tui.menu import _get_app_default_for_kind

    class FakeApp:
        _config = Config.model_validate({"app_defaults": {"pdf": "skim"}})

    assert _get_app_default_for_kind(FakeApp(), "pdf") == "skim"  # type: ignore[arg-type]
    assert _get_app_default_for_kind(FakeApp(), "md") == ""  # type: ignore[arg-type]

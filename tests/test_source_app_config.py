"""Phase 2: per-source app fields + Config-level cross-validation.

The new optional fields on :class:`fnd.config.SourceConfig`:

* ``app`` — single app id (sugar for "use this app for every filetype it
  handles").
* ``app_for`` — explicit per-filetype mapping (``{"md": "obsidian"}``).
* ``app_params`` — free-form key/value bag for template variables
  (``vault``, custom user vars).

References to unknown app ids are rejected at the top-level
:class:`Config` model_validator (sources can't see siblings).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from fnd.config import Config, SourceConfig


def test_source_accepts_app() -> None:
    src = SourceConfig(path=Path("/tmp/notes"), app="obsidian")
    assert src.app == "obsidian"


def test_source_accepts_app_for_mapping() -> None:
    src = SourceConfig(
        path=Path("/tmp/notes"),
        app_for={"md": "obsidian", "pdf": "skim"},
    )
    assert src.app_for == {"md": "obsidian", "pdf": "skim"}


def test_source_accepts_app_params() -> None:
    src = SourceConfig(
        path=Path("/tmp/notes"),
        app="obsidian",
        app_params={"vault": "MyVault"},
    )
    assert src.app_params == {"vault": "MyVault"}


def test_source_defaults_have_empty_app_fields() -> None:
    src = SourceConfig(path=Path("/tmp/x"))
    assert src.app is None
    assert src.app_for == {}
    assert src.app_params == {}


# ── Config-level validation of app references ───────────────────────────


def _cfg_with_source(**source_kwargs: object) -> dict[str, object]:
    return {
        "collections": {
            "default": {
                "sources": [{"path": "/tmp/x", **source_kwargs}],
            }
        }
    }


def test_config_accepts_known_builtin_app_in_source_app() -> None:
    Config.model_validate(_cfg_with_source(app="obsidian"))


def test_config_accepts_known_builtin_app_in_source_app_for() -> None:
    Config.model_validate(_cfg_with_source(app_for={"md": "obsidian", "pdf": "skim"}))


def test_config_rejects_unknown_app_in_source_app() -> None:
    with pytest.raises(ValidationError, match=r"app"):
        Config.model_validate(_cfg_with_source(app="ghost"))


def test_config_rejects_unknown_app_in_source_app_for() -> None:
    with pytest.raises(ValidationError, match=r"app_for"):
        Config.model_validate(_cfg_with_source(app_for={"md": "ghost"}))


def test_config_user_app_id_visible_to_source_validation() -> None:
    """A source can reference an id defined in [apps.<id>] — not just
    built-ins."""
    Config.model_validate(
        {
            "apps": {
                "marked": {
                    "display_name": "Marked",
                    "handles": ["md"],
                    "argv": ["open", "-a", "Marked 2", "{path}"],
                }
            },
            **_cfg_with_source(app="marked"),
        }
    )


def test_config_rejects_unknown_filetype_in_source_app_for() -> None:
    with pytest.raises(ValidationError, match=r"banana|app_for"):
        Config.model_validate(_cfg_with_source(app_for={"banana": "obsidian"}))


# ── OpenRequest gets template variables from source ────────────────────


def test_open_smart_passes_vault_through_to_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When a source carries ``app_params.vault`` and the resolved app is
    Obsidian, ``open_smart`` should build the right URL."""
    from types import SimpleNamespace

    from fnd import apps as apps_mod
    from fnd import opener as opener_mod

    md_file = tmp_path / "note.md"
    md_file.write_text("# Hi\n")

    captured: list[list[str]] = []
    # Capture at the launcher's open-url seam (OS-independent) rather than the
    # per-OS opener command, so these URL assertions read the same on every OS.
    monkeypatch.setattr(
        apps_mod.launcher, "open_url", lambda url: captured.append(["open", url]) or 0
    )
    monkeypatch.setattr(apps_mod, "_obsidian_app_exists", lambda: True)
    # Make config loading return a minimal Config that defaults md→obsidian.
    # opener.open_smart does ``from fnd.config import load as load_config``
    # at call time — patch the source attribute so the lookup sees the stub.
    import fnd.config as cfg_mod

    monkeypatch.setattr(
        cfg_mod,
        "load",
        lambda *args, **kw: Config.model_validate({"app_defaults": {"md": "obsidian"}}),
    )

    src = SimpleNamespace(
        app="obsidian",
        app_for={},
        app_params={"vault": "MyVault"},
        path=tmp_path,
    )
    opener_mod.open_smart(
        path=md_file,
        kind="md",
        source=src,
        heading_path="Hi",
    )
    assert len(captured) == 1
    argv = captured[0]
    assert argv[0] == "open"
    assert argv[1].startswith("obsidian://open")
    assert "vault=MyVault" in argv[1]
    assert "%23Hi" in argv[1]  # heading appended as URL-encoded #

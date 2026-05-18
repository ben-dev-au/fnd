"""Recovery flow shown when ``Config.load()`` fails at TUI startup."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from fnd.config import Config
from fnd.tui.config_recovery_screen import _backup_name, _format_error


def test_format_toml_decode_error_includes_path_and_message(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text("this is not [valid", encoding="utf-8")
    try:
        tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        text = _format_error(e, p)
        assert str(p) in text
        assert "TOML parse error" in text
        return
    pytest.fail("expected TOMLDecodeError")


def test_format_validation_error_lists_field_paths(tmp_path: Path) -> None:
    try:
        Config.model_validate({"defaults": {"result_limit": "huge"}})
    except ValidationError as e:
        text = _format_error(e, tmp_path / "config.toml")
        assert "Schema validation error" in text
        assert "defaults" in text
        assert "result_limit" in text
        return
    pytest.fail("expected ValidationError")


def test_format_other_exception_falls_through(tmp_path: Path) -> None:
    text = _format_error(RuntimeError("disk on fire"), tmp_path / "config.toml")
    assert "disk on fire" in text


def test_backup_name_is_timestamped(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    name = _backup_name(p).name
    assert name.startswith("config.toml.bak-")
    # YYYYMMDDTHHMMSSZ format; 16 chars after the prefix.
    suffix = name.removeprefix("config.toml.bak-")
    assert len(suffix) == 16
    assert suffix.endswith("Z")

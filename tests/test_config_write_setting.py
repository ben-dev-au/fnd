"""`write_setting` updates one config field via dotted path and preserves
user comments. Used by the Settings menu's inline editors."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from acorn.config import load, write_setting


def test_writes_scalar_into_empty_file(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("", encoding="utf-8")
    cfg = write_setting(
        config_path=cfg_path,
        dotted_path="defaults.result_limit",
        value=42,
    )
    assert cfg.defaults.result_limit == 42
    assert load(cfg_path).defaults.result_limit == 42


def test_preserves_user_comments(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    original = textwrap.dedent(
        """
        # my custom limit comment
        [defaults]
        # before
        result_limit = 100
        # after
        """
    ).lstrip()
    cfg_path.write_text(original, encoding="utf-8")
    write_setting(config_path=cfg_path, dotted_path="defaults.result_limit", value=250)
    after = cfg_path.read_text(encoding="utf-8")
    assert "# my custom limit comment" in after
    assert "# before" in after
    assert "# after" in after
    assert "result_limit = 250" in after


def test_creates_nested_tables_on_the_fly(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("", encoding="utf-8")
    write_setting(
        config_path=cfg_path,
        dotted_path="ranking.default.recency_boost",
        value=0.5,
    )
    cfg = load(cfg_path)
    assert cfg.ranking["default"].recency_boost == 0.5


def test_rejects_invalid_value_and_leaves_file_unchanged(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    original = textwrap.dedent(
        """
        [defaults]
        result_limit = 100
        """
    ).lstrip()
    cfg_path.write_text(original, encoding="utf-8")
    with pytest.raises(ValidationError):
        write_setting(
            config_path=cfg_path,
            dotted_path="defaults.result_limit",
            value="not an integer",
        )
    # File is unchanged.
    assert cfg_path.read_text(encoding="utf-8") == original


def test_repeated_writes_preserve_comments(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(
            """
            # my custom limit
            [defaults]
            result_limit = 100
            """
        ).lstrip(),
        encoding="utf-8",
    )
    for v in (200, 300, 400):
        write_setting(config_path=cfg_path, dotted_path="defaults.result_limit", value=v)
    after = cfg_path.read_text(encoding="utf-8")
    assert "# my custom limit" in after
    assert "result_limit = 400" in after

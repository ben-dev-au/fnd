"""tag_sources config field and its write path."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from fnd.config import Config, Defaults, write_setting


def test_defaults_to_both_sources() -> None:
    assert Defaults().tag_sources == ["frontmatter", "os"]


def test_accepts_a_single_source() -> None:
    assert Defaults(tag_sources=["frontmatter"]).tag_sources == ["frontmatter"]


def test_accepts_an_empty_list() -> None:
    """Disabling every source is legitimate: it turns the tag pane off."""
    assert Defaults(tag_sources=[]).tag_sources == []


def test_rejects_an_unknown_source() -> None:
    with pytest.raises(ValidationError, match="Input should be"):
        Defaults(tag_sources=["frontmatter", "telepathy"])  # type: ignore[list-item]


def test_round_trips_through_write_setting(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("", encoding="utf-8")
    updated: Config = write_setting(
        config_path=cfg_path, dotted_path="defaults.tag_sources", value=["frontmatter"]
    )
    assert updated.defaults.tag_sources == ["frontmatter"]
    assert "tag_sources" in cfg_path.read_text(encoding="utf-8")

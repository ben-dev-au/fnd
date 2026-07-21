"""The shipped starter config must be generic — no developer's personal setup."""

from __future__ import annotations

import tomllib
from pathlib import Path

from fnd.config import CONFIG_TEMPLATE, Config, Defaults


def test_template_parses() -> None:
    Config.model_validate(tomllib.loads(CONFIG_TEMPLATE))


def test_template_yields_default_tag_settings() -> None:
    """A new user starts with both tag sources on and no custom keys."""
    cfg = Config.model_validate(tomllib.loads(CONFIG_TEMPLATE))
    assert cfg.defaults.tag_sources == ["frontmatter", "os"]
    assert cfg.defaults.tag_frontmatter_keys == []
    assert cfg.defaults.tag_frontmatter_keys == Defaults().tag_frontmatter_keys


def test_template_carries_no_personal_paths() -> None:
    lowered = CONFIG_TEMPLATE.lower()
    for leak in ("/users/", "bendavidson", "obsidian vault", "icloud~md~obsidian"):
        assert leak not in lowered, f"template leaks {leak!r}"


def test_template_carries_no_personal_collections() -> None:
    """Course-code collections from a real setup must never ship."""
    lowered = CONFIG_TEMPLATE.lower()
    for leak in ("dpc", "cpl", "sfo", "ssd", "wbt", "dsa", "notes_type", "uni week"):
        assert leak not in lowered, f"template leaks {leak!r}"


def test_template_documents_the_tag_settings() -> None:
    """Discoverable without reading source."""
    assert "tag_sources" in CONFIG_TEMPLATE
    assert "tag_frontmatter_keys" in CONFIG_TEMPLATE


def test_a_bare_config_has_clean_tag_defaults(tmp_path: Path) -> None:
    """Even an empty file must not inherit anything."""
    from fnd.config import load

    p = tmp_path / "config.toml"
    p.write_text("", encoding="utf-8")
    cfg = load(p)
    assert cfg.defaults.tag_frontmatter_keys == []
    assert cfg.defaults.tag_sources == ["frontmatter", "os"]

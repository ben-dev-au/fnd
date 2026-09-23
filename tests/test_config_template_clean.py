"""The generated starter config must be generic: no developer's personal setup."""

from __future__ import annotations

from pathlib import Path

from fnd.config import Defaults, load, starter_config


def test_template_parses(tmp_path: Path) -> None:
    """Through load, which strips config_version; model_validate would only
    pass because Config ignores unknown keys."""
    p = tmp_path / "config.toml"
    p.write_text(starter_config(), encoding="utf-8")
    load(p)


def test_template_yields_default_tag_settings(tmp_path: Path) -> None:
    """A new user starts with both tag sources on and no custom keys."""
    p = tmp_path / "config.toml"
    p.write_text(starter_config(), encoding="utf-8")
    cfg = load(p)
    assert cfg.defaults.tag_sources == ["frontmatter", "os"]
    assert cfg.defaults.tag_frontmatter_keys == []
    assert cfg.defaults.tag_frontmatter_keys == Defaults().tag_frontmatter_keys


def test_template_carries_no_personal_paths() -> None:
    lowered = starter_config().lower()
    for leak in ("/users/", "bendavidson", "obsidian vault", "icloud~md~obsidian"):
        assert leak not in lowered, f"template leaks {leak!r}"


def test_template_carries_no_personal_collections() -> None:
    """Course-code collections from a real setup must never ship."""
    lowered = starter_config().lower()
    for leak in ("dpc", "cpl", "sfo", "ssd", "wbt", "dsa", "notes_type", "uni week"):
        assert leak not in lowered, f"template leaks {leak!r}"


def test_template_documents_the_tag_settings() -> None:
    """Discoverable without reading source."""
    template = starter_config()
    assert "tag_sources" in template
    assert "tag_frontmatter_keys" in template


def test_a_bare_config_has_clean_tag_defaults(tmp_path: Path) -> None:
    """Even an empty file must not inherit anything."""
    from fnd.config import load

    p = tmp_path / "config.toml"
    p.write_text("", encoding="utf-8")
    cfg = load(p)
    assert cfg.defaults.tag_frontmatter_keys == []
    assert cfg.defaults.tag_sources == ["frontmatter", "os"]

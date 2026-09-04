"""Formatting of the TOML the settings UI writes."""

from __future__ import annotations

from pathlib import Path

from fnd.config import write_setting


def _write(path: Path) -> str:
    write_setting(config_path=path, dotted_path="defaults.filters.kinds", value=["md"])
    write_setting(config_path=path, dotted_path="defaults.filters.max_size", value=50_000_000)
    return path.read_text(encoding="utf-8")


def test_a_new_table_does_not_abut_the_next_one(tmp_path: Path) -> None:
    """tomlkit renders a table created this run flush against its successor."""
    cfg = tmp_path / "config.toml"
    cfg.write_text('[defaults]\nresult_limit = 50\n[[collections.a.sources]]\npath = "~/x"\n')
    lines = _write(cfg).splitlines()
    for i, line in enumerate(lines):
        if line.startswith("[") and i:
            assert lines[i - 1].strip() == "", f"no blank line before {line!r}"


def test_the_spacing_is_idempotent(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('[defaults]\nresult_limit = 50\n[[collections.a.sources]]\npath = "~/x"\n')
    assert _write(cfg) == _write(cfg)


def test_a_comment_stays_attached_to_its_table(tmp_path: Path) -> None:
    """The blank goes above the comment block, not between it and the table."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[defaults]\nresult_limit = 50\n\n# what this collection is for\n"
        '[[collections.a.sources]]\npath = "~/x"\n'
    )
    lines = _write(cfg).splitlines()
    i = lines.index("[[collections.a.sources]]")
    assert lines[i - 1] == "# what this collection is for"


def test_a_byte_size_is_written_with_digit_groups(tmp_path: Path) -> None:
    """``50000000`` is not a number anyone reads at a glance."""
    cfg = tmp_path / "config.toml"
    cfg.write_text("[defaults]\nresult_limit = 50\n")
    write_setting(config_path=cfg, dotted_path="defaults.filters.max_size", value=50_000_000)
    write_setting(config_path=cfg, dotted_path="defaults.result_limit", value=200)
    text = cfg.read_text(encoding="utf-8")
    assert "max_size = 50_000_000" in text
    assert "result_limit = 200" in text, "small numbers must stay plain"
    from fnd.config import load

    assert load(cfg).defaults.filters.max_size == 50_000_000

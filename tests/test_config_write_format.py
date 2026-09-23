"""Formatting of the TOML the settings UI writes."""

from __future__ import annotations

import re
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


def test_every_commented_key_can_be_uncommented_and_still_load(tmp_path: Path) -> None:
    """The renderer offers each unset key as a commented example, so each one
    must sit in the table it belongs to: uncommenting it is the documented way
    to change a setting, and a misplaced key fails the whole config."""
    from fnd.config import load, starter_config

    cfg = tmp_path / "config.toml"
    text = starter_config()
    lines = text.splitlines()
    checked = 0
    key_line = re.compile(r"# ([A-Za-z_][A-Za-z0-9_]*) = .+")
    in_example = False
    for n, line in enumerate(lines):
        if not line.strip():
            in_example = False
        elif line.startswith("# ["):
            # A commented-out table is an example uncommented as a block.
            in_example = True
        if in_example or not key_line.fullmatch(line.rstrip()):
            continue
        body = line[2:].rstrip()
        cfg.write_text("\n".join([*lines[:n], body, *lines[n + 1 :]]), encoding="utf-8")
        load(cfg)  # raises if the key landed in the wrong table
        checked += 1
    # Derived, not a fixed floor: a renderer that stopped emitting commented
    # examples would still clear an arbitrary number. A field defaulting to
    # None renders as a bare `# key =`, which is not uncommentable, so only
    # fields with a value count. `filters` is a table, not a key.
    from fnd.config import DefaultFilters, Defaults

    def with_values(model: type) -> int:
        return sum(
            1
            for name, info in model.model_fields.items()
            if name != "filters" and info.get_default(call_default_factory=True) is not None
        )

    expected = with_values(Defaults) + with_values(DefaultFilters)
    assert checked >= expected, f"only {checked} commented keys exercised, expected {expected}"


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

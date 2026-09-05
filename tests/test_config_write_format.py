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


def test_a_commented_out_key_stays_in_its_own_table(tmp_path: Path) -> None:
    """A new sub-table must not be inserted above the parent's trailing
    comments. The shipped template documents optional keys that way, and a
    relocated one, once uncommented, lands in ``[defaults.filters]`` where it
    is not a valid key — the whole config then fails to load."""
    from fnd.config import load

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[defaults]\nresult_limit = 50\n"
        '# tag_frontmatter_keys = ["Course"]\n\n'
        '[[collections.a.sources]]\npath = "~/x"\n'
    )
    text = _write(cfg)
    body, _, _rest = text.partition("[defaults.filters]")
    assert "tag_frontmatter_keys" in body, "the comment was moved into the sub-table"

    uncommented = text.replace(
        '# tag_frontmatter_keys = ["Course"]', 'tag_frontmatter_keys = ["Course"]'
    )
    cfg.write_text(uncommented)
    assert load(cfg).defaults.tag_frontmatter_keys == ["Course"]


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


def test_only_a_real_table_header_gets_a_blank_line(tmp_path: Path) -> None:
    """The spacing pass rewrites the whole document, so it must not mistake a
    value for a header — a multi-line string can contain a line that looks
    exactly like one, and a blank inserted there changes the value."""
    from fnd.config import _spaced_tables

    quotes = '"""'
    unchanged = [
        "[section] not a table\nmore\n",
        f"a = {quotes}\n[not a table]\nstill\n{quotes}\nb = 1\n",
        "[one]\nk = 1\n\n[two]\nj = 2\n",
    ]
    for text in unchanged:
        assert _spaced_tables(text) == text, text
    assert _spaced_tables("[one]\nk = 1\n[two]\nj = 2\n") == "[one]\nk = 1\n\n[two]\nj = 2\n"

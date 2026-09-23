"""The CLI points out a duplicate or nested source, as the source form does.

The TUI will not launch without an index, so a first collection is often set
up through the CLI; the overlap warning is one shared helper, not a copy.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fnd.cli import app
from fnd.config import SourceConfig, overlapping_source


def test_the_helper_is_shared_not_copied() -> None:
    """One implementation, imported by both callers."""
    from fnd.tui import settings_screen

    assert "def overlapping_source" not in Path("fnd/tui/settings_screen.py").read_text(
        encoding="utf-8"
    )
    assert settings_screen is not None


def test_it_still_finds_a_nested_folder(tmp_path: Path) -> None:
    parent = tmp_path / "vault"
    (parent / "notes").mkdir(parents=True)

    # This returns `(path, relation)`; a non-empty tuple is always truthy, so
    # a bare assert would test nothing.
    found, _contains = overlapping_source(
        [SourceConfig(path=parent)], SourceConfig(path=parent / "notes")
    )
    assert found


@pytest.fixture
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "vault"
    (root / "notes").mkdir(parents=True)
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.notes.sources]]
            path = "{root.as_posix()}"
        """),
        encoding="utf-8",
    )
    # `fnd.cli` binds this name at import, so patching `fnd.config`'s copy
    # leaves the CLI pointed at the real config, which a failing test would
    # then write sources into.
    monkeypatch.setattr("fnd.cli.default_config_path", lambda: cfg_path)
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return root


def test_adding_a_nested_source_warns(configured: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["collection", "add", "notes", "--source", str(configured / "notes")],
    )

    assert result.exit_code == 0, result.output
    assert "already inside" in result.output, result.output


def test_adding_a_separate_folder_does_not(configured: Path, tmp_path: Path) -> None:
    """The control: an unrelated folder must add in silence."""
    other = tmp_path / "elsewhere"
    other.mkdir()

    result = CliRunner().invoke(app, ["collection", "add", "notes", "--source", str(other)])

    assert result.exit_code == 0, result.output
    assert "already inside" not in result.output, result.output

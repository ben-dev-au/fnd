"""`fnd collection add` writes [[sources]] via tomlkit."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fnd.cli import app
from fnd.config import load


def _runner_with_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, initial: str = ""
) -> tuple[CliRunner, Path]:
    cfg_path = tmp_path / "config.toml"
    if initial:
        cfg_path.write_text(textwrap.dedent(initial), encoding="utf-8")
    else:
        cfg_path.write_text("", encoding="utf-8")
    # Force the CLI to use the temp config file.
    monkeypatch.setattr("fnd.cli.default_config_path", lambda: cfg_path)
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return CliRunner(), cfg_path


def test_collection_add_minimal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner, cfg_path = _runner_with_config(monkeypatch, tmp_path)
    notes = tmp_path / "notes"
    notes.mkdir()
    result = runner.invoke(app, ["collection", "add", "coursework", "--source", str(notes)])
    assert result.exit_code == 0, result.output
    cfg = load(cfg_path)
    cw = cfg.collection("coursework")
    assert len(cw.sources) == 1
    assert cw.sources[0].path == notes


def test_collection_add_with_filter_and_globs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner, cfg_path = _runner_with_config(monkeypatch, tmp_path)
    notes = tmp_path / "notes"
    notes.mkdir()
    result = runner.invoke(
        app,
        [
            "collection",
            "add",
            "coursework",
            "--source",
            str(notes),
            "--include",
            "**/*.md",
            "--exclude",
            "**/.trash/**",
            "--filter",
            "Course == 'DPwC'",
        ],
    )
    assert result.exit_code == 0, result.output
    s = load(cfg_path).collection("coursework").sources[0]
    assert s.filters.kinds == ["md"]
    assert s.excludes == ["**/.trash/**"]
    assert s.frontmatter_filter == "Course == 'DPwC'"


def test_collection_add_invalid_filter_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner, cfg_path = _runner_with_config(monkeypatch, tmp_path)
    notes = tmp_path / "notes"
    notes.mkdir()
    result = runner.invoke(
        app,
        [
            "collection",
            "add",
            "coursework",
            "--source",
            str(notes),
            "--filter",
            "Course ==",
        ],
    )
    assert result.exit_code != 0
    assert "col" in result.output.lower()
    # Config file unchanged.
    assert "coursework" not in cfg_path.read_text(encoding="utf-8")


def test_collection_add_appends_to_existing_collection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Adding `--source` to an existing collection appends, doesn't replace."""
    initial = """
        [[collections.coursework.sources]]
        path = "/tmp/notes"
        includes = ["**/*.md"]
    """
    runner, cfg_path = _runner_with_config(monkeypatch, tmp_path, initial)
    pdfs = tmp_path / "pdfs"
    pdfs.mkdir()
    result = runner.invoke(
        app,
        ["collection", "add", "coursework", "--source", str(pdfs), "--include", "**/*.pdf"],
    )
    assert result.exit_code == 0, result.output
    cw = load(cfg_path).collection("coursework")
    assert len(cw.sources) == 2
    assert cw.sources[1].filters.kinds == ["pdf"]


def test_collection_add_preserves_user_comments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    initial = """
        # I love this collection.
        [defaults]
        # global default
        collection = "coursework"
    """
    runner, cfg_path = _runner_with_config(monkeypatch, tmp_path, initial)
    notes = tmp_path / "notes"
    notes.mkdir()
    result = runner.invoke(app, ["collection", "add", "coursework", "--source", str(notes)])
    assert result.exit_code == 0, result.output
    text = cfg_path.read_text(encoding="utf-8")
    assert "# I love this collection." in text
    assert "# global default" in text


def test_collection_list_counts_sources_not_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After 5.5e-1, the canonical attribute is `sources` (legacy `roots` is
    cleared by the normaliser). `collection list` must report source count
    so users with the new schema see a meaningful number."""
    runner, _cfg_path = _runner_with_config(
        monkeypatch,
        tmp_path,
        """
        [[collections.coursework.sources]]
        path = "/tmp/notes"
        includes = ["**/*.md"]

        [[collections.coursework.sources]]
        path = "/tmp/papers"
        includes = ["**/*.pdf"]
    """,
    )
    result = runner.invoke(app, ["collection", "list"])
    assert result.exit_code == 0, result.output
    assert "coursework" in result.output
    # Two sources configured; output must show 2, not 0.
    assert "2" in result.output
    assert "source" in result.output.lower()

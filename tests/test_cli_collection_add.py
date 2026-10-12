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
    result = runner.invoke(app, ["collection", "add", "learning", "--source", str(notes)])
    assert result.exit_code == 0, result.output
    cfg = load(cfg_path)
    cw = cfg.collection("learning")
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
            "learning",
            "--source",
            str(notes),
            "--include",
            "**/*.md",
            "--exclude",
            "**/.trash/**",
            "--filter",
            "Project == 'Website'",
        ],
    )
    assert result.exit_code == 0, result.output
    s = load(cfg_path).collection("learning").sources[0]
    assert s.includes == ["**/*.md"]
    assert s.excludes == ["**/.trash/**"]
    # Asserted where the rule takes effect, not where it is stored: a new
    # write uses `filters.frontmatter`, the deprecated key still loads.
    assert s.effective_filters.frontmatter == "Project == 'Website'"


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
            "learning",
            "--source",
            str(notes),
            "--filter",
            "Project ==",
        ],
    )
    assert result.exit_code != 0
    assert "col" in result.output.lower()
    # Config file unchanged.
    assert "learning" not in cfg_path.read_text(encoding="utf-8")


def test_collection_add_appends_to_existing_collection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Adding `--source` to an existing collection appends, doesn't replace."""
    initial = """
        [[collections.learning.sources]]
        path = "/tmp/notes"
        includes = ["**/*.md"]
    """
    runner, cfg_path = _runner_with_config(monkeypatch, tmp_path, initial)
    pdfs = tmp_path / "pdfs"
    pdfs.mkdir()
    result = runner.invoke(
        app,
        ["collection", "add", "learning", "--source", str(pdfs), "--include", "**/*.pdf"],
    )
    assert result.exit_code == 0, result.output
    cw = load(cfg_path).collection("learning")
    assert len(cw.sources) == 2
    # ``pdf`` has one suffix, so this glob is the whole kind and folds.
    assert cw.sources[1].filters is not None
    assert cw.sources[1].filters.kinds == ["pdf"]


def test_collection_add_keeps_the_notes_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A write regenerates the file, so notes are where a user's own text
    survives; anything outside is replaced by the generated documentation."""
    initial = """
        # >>> notes: kept verbatim when fnd rewrites this file
        # I love this collection.
        # <<< notes
        [defaults]
        collection = "learning"
    """
    runner, cfg_path = _runner_with_config(monkeypatch, tmp_path, initial)
    notes = tmp_path / "notes"
    notes.mkdir()
    result = runner.invoke(app, ["collection", "add", "learning", "--source", str(notes)])
    assert result.exit_code == 0, result.output
    text = cfg_path.read_text(encoding="utf-8")
    assert "# I love this collection." in text
    assert 'collection = "learning"' in text


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
        [[collections.learning.sources]]
        path = "/tmp/notes"
        includes = ["**/*.md"]

        [[collections.learning.sources]]
        path = "/tmp/papers"
        includes = ["**/*.pdf"]
    """,
    )
    result = runner.invoke(app, ["collection", "list"])
    assert result.exit_code == 0, result.output
    assert "learning" in result.output
    # Two sources configured; output must show 2, not 0.
    assert "2" in result.output
    assert "source" in result.output.lower()


def test_the_source_help_matches_what_the_command_accepts() -> None:
    """It said "Repeat to add multiple" while the command refuses a second
    one, and the docstring three lines above said the opposite."""
    import re

    from rich.text import Text
    from typer.testing import CliRunner

    from fnd.cli import app

    # Typer forces colour under GITHUB_ACTIONS, and a style code can split a phrase.
    out = Text.from_ansi(CliRunner().invoke(app, ["collection", "add", "--help"]).stdout).plain
    flat = " ".join(re.sub(r"[│─╭╮╰╯]", " ", out).split())
    assert "Repeat to add multiple" not in flat
    assert "One per command" in flat, flat[:200]


def test_no_message_points_at_a_command_that_does_not_exist() -> None:
    """`fnd status --errors` was named as the way to see dropped files; the
    option does not exist, and nothing reports them."""
    import inspect

    from typer.testing import CliRunner

    from fnd import walk
    from fnd.cli import app

    assert "status --errors" not in inspect.getsource(walk)
    assert CliRunner().invoke(app, ["status", "--errors"]).exit_code != 0


def test_collection_add_writes_excludes_into_the_sources_filters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The CLI writes the current place, so no later migration has to move it."""
    import tomllib

    runner, cfg_path = _runner_with_config(monkeypatch, tmp_path)
    notes = tmp_path / "n"
    notes.mkdir()
    result = runner.invoke(
        app, ["collection", "add", "c", "--source", str(notes), "--exclude", "**/.trash/**"]
    )
    assert result.exit_code == 0, result.output
    source = tomllib.loads(cfg_path.read_text(encoding="utf-8"))["collections"]["c"]["sources"][0]
    assert "excludes" not in source
    assert source["filters"]["excludes"] == ["**/.trash/**"]


def test_collection_add_with_excluded_folders(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--exclude-dir is repeatable and lands on the source's filters, leaving excludes inherited."""
    runner, cfg_path = _runner_with_config(monkeypatch, tmp_path)
    notes = tmp_path / "notes"
    notes.mkdir()
    result = runner.invoke(
        app,
        [
            "collection",
            "add",
            "learning",
            "--source",
            str(notes),
            "--exclude-dir",
            "archive/",
            "--exclude-dir",
            "projects/old",
        ],
    )
    assert result.exit_code == 0, result.output
    source = load(cfg_path).collection("learning").sources[0]
    assert source.exclude_dirs == ["archive", "projects/old"]
    assert source.filters is not None
    assert source.filters.excludes is None


def test_collection_add_refuses_a_folder_outside_the_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A path that escapes the source exits 1 and says why."""
    runner, _cfg = _runner_with_config(monkeypatch, tmp_path)
    notes = tmp_path / "notes"
    notes.mkdir()
    result = runner.invoke(
        app, ["collection", "add", "learning", "--source", str(notes), "--exclude-dir", "../x"]
    )
    assert result.exit_code == 1
    assert "must stay inside the source folder" in result.output


def test_collection_add_warns_when_an_excluded_folder_is_not_there(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A typo or a wrong-case name is accepted but said to exclude nothing."""
    runner, cfg_path = _runner_with_config(monkeypatch, tmp_path)
    notes = tmp_path / "notes"
    (notes / "archive").mkdir(parents=True)
    result = runner.invoke(
        app,
        [
            "collection",
            "add",
            "l",
            "--source",
            str(notes),
            "--exclude-dir",
            "Archive",
            "--exclude-dir",
            "archive",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "'Archive' is not a folder under the source" in result.output
    assert "'archive' is not" not in result.output
    assert load(cfg_path).collection("l").sources[0].exclude_dirs == ["Archive", "archive"]


def test_collection_add_does_not_warn_about_a_source_it_then_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A name clash exits 1 before any folder warning, since nothing is written."""
    runner, _cfg = _runner_with_config(monkeypatch, tmp_path)
    notes = tmp_path / "notes"
    notes.mkdir()
    assert (
        runner.invoke(app, ["collection", "add", "Learning", "--source", str(notes)]).exit_code == 0
    )
    result = runner.invoke(
        app,
        ["collection", "add", "learning", "--source", str(notes), "--exclude-dir", "missing"],
    )
    assert result.exit_code == 1
    assert "is not a folder under the source" not in result.output

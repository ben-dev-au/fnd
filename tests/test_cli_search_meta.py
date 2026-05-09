"""Phase 5.5e-2: `acorn search --meta` filters at query time."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from acorn.cli import app
from acorn.config import CollectionConfig, SourceConfig
from acorn.index import build_index_from_config


def _touch(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture
def cli_corpus(tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    notes = tmp_path / "notes"
    _touch(notes / "dpwc.md", "---\nCourse: DPwC\n---\n# A\nlightning rod\n")
    _touch(notes / "other.md", "---\nCourse: Other\n---\n# B\nlightning rod\n")
    cc = CollectionConfig(sources=[SourceConfig(path=notes, includes=["**/*.md"])])
    build_index_from_config(config=cc, collection="notes", index_dir=tmp_index_dir)
    monkeypatch.setattr("acorn.cli.default_index_dir", lambda: tmp_index_dir)
    return tmp_index_dir


def test_search_meta_flag_filters_results(cli_corpus: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "search",
            "lightning rod",
            "--collection",
            "notes",
            "--meta",
            "Course == 'DPwC'",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "dpwc.md" in result.output
    assert "other.md" not in result.output


def test_search_no_meta_returns_both(cli_corpus: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["search", "lightning rod", "--collection", "notes"])
    assert result.exit_code == 0
    assert "dpwc.md" in result.output
    assert "other.md" in result.output


def test_search_meta_invalid_filter_exits_nonzero(cli_corpus: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "search",
            "lightning rod",
            "--collection",
            "notes",
            "--meta",
            "Course ==",
        ],
    )
    assert result.exit_code != 0
    assert "col" in result.output.lower()

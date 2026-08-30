"""`fnd search` must report malformed queries cleanly, never crash with a traceback."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from fnd.cli import app
from fnd.config import CollectionConfig, SourceConfig
from fnd.index import build_index_from_config


@pytest.fixture
def cli_corpus(tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    notes = tmp_path / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "a.md").write_text("# A\nbuffer overflow exploit here\n", encoding="utf-8")
    cc = CollectionConfig(sources=[SourceConfig(path=notes, includes=["**/*.md"])])
    build_index_from_config(config=cc, collection="notes", index_dir=tmp_index_dir)
    monkeypatch.setattr("fnd.cli.default_index_dir", lambda: tmp_index_dir)
    return tmp_index_dir


def test_standalone_proximity_reports_cleanly(cli_corpus: Path) -> None:
    result = runner_invoke(["search", "{60}", "--collection", "notes"])
    assert result.exit_code != 0
    assert "proximity" in result.output.lower()
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_unbalanced_quote_reports_cleanly(cli_corpus: Path) -> None:
    result = runner_invoke(["search", '"unbalanced', "--collection", "notes"])
    assert result.exit_code != 0
    assert "syntax" in result.output.lower()


def test_valid_proximity_succeeds(cli_corpus: Path) -> None:
    result = runner_invoke(["search", "{60} buffer overflow exploit", "--collection", "notes"])
    assert result.exit_code == 0, result.output


def runner_invoke(args: list[str]) -> Result:
    return CliRunner().invoke(app, args)

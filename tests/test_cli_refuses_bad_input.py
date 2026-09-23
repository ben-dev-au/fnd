"""A command that cannot do what it was asked says so, and exits non-zero.

Two paths leaked an interpreter traceback instead: `collection reindex` on a
config with no collections reached `cfg.collection()` as a raw KeyError, and
`--limit 0` panicked out of Rust carrying a build path from the machine the
wheel was compiled on.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from fnd.cli import app

runner = CliRunner()


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[defaults]\n", encoding="utf-8")
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    monkeypatch.setattr("fnd.cli.default_index_dir", lambda: tmp_path / "idx")
    return cfg_path


def test_reindexing_with_no_collections_configured_says_so(sandbox: Path) -> None:
    result = runner.invoke(app, ["collection", "reindex", "-c", "notes"])
    assert result.exit_code == 1
    assert "Traceback" not in result.output, result.output
    assert "no collections configured" in result.output


def test_reindexing_an_unknown_name_names_it(sandbox: Path) -> None:
    sandbox.write_text(
        '[defaults]\n\n[[collections.notes.sources]]\npath = "/tmp"\n', encoding="utf-8"
    )
    result = runner.invoke(app, ["collection", "reindex", "-c", "notez"])
    assert result.exit_code != 0
    assert "Traceback" not in result.output, result.output
    assert "notez" in result.output


@pytest.mark.parametrize("bad", ["0", "-3"])
def test_a_limit_below_one_is_refused(sandbox: Path, bad: str) -> None:
    result = runner.invoke(app, ["search", "anything", "--limit", bad])
    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output, result.output
    assert "PanicException" not in result.output
    assert "--limit must be 1 or more" in result.output


def test_a_real_limit_still_works(sandbox: Path) -> None:
    """The control: the guard must not refuse an ordinary search."""
    result = runner.invoke(app, ["search", "anything", "--limit", "5"])
    assert "--limit must be" not in result.output, result.output

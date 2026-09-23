"""`fnd collection reindex` on an index at an older schema asks for `--rebuild`
and fails, rather than reporting `indexed 0 chunks`."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fnd.cli import app


@pytest.fixture
def stale(tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    """One indexed collection whose index sidecar then claims schema 9."""
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("# A\n\nhaystack\n", encoding="utf-8")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.vault.sources]]
            path = "{notes.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.cli.default_config_path", lambda: cfg_path)
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    monkeypatch.setattr("fnd.cli.default_index_dir", lambda: tmp_index_dir)
    runner = CliRunner()
    first = runner.invoke(app, ["collection", "reindex", "vault"])
    assert first.exit_code == 0, first.output
    (tmp_index_dir / ".fnd-schema-version").write_text("9", encoding="utf-8")
    return runner


def test_a_reindex_on_an_older_schema_asks_for_a_rebuild_and_fails(stale: CliRunner) -> None:
    """The refusal reaches the user as a `--rebuild` instruction and a non-zero exit."""
    result = stale.invoke(app, ["collection", "reindex", "vault"])

    assert result.exit_code != 0, result.output
    assert "--rebuild" in result.output, result.output
    assert "indexed 0 chunks" not in result.output, result.output


def test_the_rebuild_it_asks_for_succeeds(stale: CliRunner) -> None:
    """The instruction works: `--rebuild` on the same index indexes the file and exits 0."""
    result = stale.invoke(app, ["collection", "reindex", "vault", "--rebuild"])

    assert result.exit_code == 0, result.output
    assert "indexed 1 chunks for collection vault" in result.output, result.output

"""CLI auto-prompts for schema rebuild before search / tui."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from acorn.cli import app
from acorn.config import CollectionConfig, SourceConfig
from acorn.index import build_index_from_config
from acorn.schema import SCHEMA_VERSION


def _touch(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture
def stale_corpus(tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a real index, then tamper with the sidecar to simulate a
    schema bump. Also write a config TOML pointing at the source so the
    rebuild flow knows what to do."""
    notes = tmp_path / "notes"
    _touch(notes / "a.md", "---\nCourse: DPwC\n---\n# A\nlightning rod\n")
    cc = CollectionConfig(sources=[SourceConfig(path=notes, includes=["**/*.md"])])
    build_index_from_config(config=cc, collection="notes", index_dir=tmp_index_dir)

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.notes.sources]]
            path = "{notes}"
            includes = ["**/*.md"]
        """),
        encoding="utf-8",
    )

    # Now make the sidecar stale.
    (tmp_index_dir / ".acorn-schema-version").write_text("1")

    monkeypatch.setattr("acorn.cli.default_index_dir", lambda: tmp_index_dir)
    monkeypatch.setattr("acorn.cli.default_config_path", lambda: cfg_path)
    monkeypatch.setattr("acorn.config.default_config_path", lambda: cfg_path)
    monkeypatch.setenv("ACORN_FORCE_TTY", "1")
    return tmp_index_dir


def test_search_prompts_rebuild_on_stale_and_proceeds(
    stale_corpus: Path,
) -> None:
    """TTY-style: provide 'y' on stdin → rebuild then search."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["search", "lightning rod", "--collection", "notes"],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    # Rebuild should have written the current version back to the sidecar.
    sidecar = stale_corpus / ".acorn-schema-version"
    assert sidecar.read_text().strip() == str(SCHEMA_VERSION)
    # Search should have actually run after the rebuild.
    assert "a.md" in result.output


def test_search_aborts_on_decline(stale_corpus: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["search", "lightning rod", "--collection", "notes"],
        input="n\n",
    )
    assert result.exit_code != 0
    assert "schema v1" in result.output
    assert "current is v" in result.output


def test_search_works_when_schema_already_current(
    tmp_path: Path,
    tmp_index_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No prompt should appear when the index is already up to date."""
    notes = tmp_path / "notes"
    _touch(notes / "a.md", "# A\nlightning rod\n")
    cc = CollectionConfig(sources=[SourceConfig(path=notes, includes=["**/*.md"])])
    build_index_from_config(config=cc, collection="notes", index_dir=tmp_index_dir)
    monkeypatch.setattr("acorn.cli.default_index_dir", lambda: tmp_index_dir)

    runner = CliRunner()
    result = runner.invoke(app, ["search", "lightning rod", "--collection", "notes"])
    assert result.exit_code == 0
    assert "schema v" not in result.output.lower()
    assert "rebuild" not in result.output.lower()

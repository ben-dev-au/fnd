"""CLI surface: `fnd search --explain N` JSON trace."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fnd.cli import app
from fnd.index import build_index


def _bootstrap_index(tmp_path: Path) -> tuple[Path, Path]:
    """Build a minimal index + config that the cli `search` command can use."""
    notes = tmp_path / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "doc.md").write_text(
        "# Doc\n\nmitochondrion is the powerhouse of the cell. "
        "mitochondrion mitochondrion mitochondrion.\n",
        encoding="utf-8",
    )
    index_dir = tmp_path / "idx"
    index_dir.mkdir(parents=True, exist_ok=True)
    build_index(roots=[notes], index_dir=index_dir, collection="notes")

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.notes.sources]]
            path = "{notes.as_posix()}"
        """),
        encoding="utf-8",
    )
    return index_dir, cfg_path


def test_search_without_explain_prints_existing_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No --explain: existing row format only, no JSON tail."""
    index_dir, cfg_path = _bootstrap_index(tmp_path)
    monkeypatch.setattr("fnd.cli.default_index_dir", lambda: index_dir)
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    monkeypatch.setattr("fnd.migrate.prompt_and_rebuild_or_exit", lambda **kw: None)

    result = CliRunner().invoke(app, ["search", "mitochondrion", "--collection", "notes"])
    assert result.exit_code == 0
    assert "powerhouse" in result.stdout
    # No JSON object should appear in the tail.
    assert '"regime"' not in result.stdout


def test_search_explain_emits_trace_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--explain 1 emits the existing rows plus a parseable JSON trace."""
    index_dir, cfg_path = _bootstrap_index(tmp_path)
    monkeypatch.setattr("fnd.cli.default_index_dir", lambda: index_dir)
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    monkeypatch.setattr("fnd.migrate.prompt_and_rebuild_or_exit", lambda **kw: None)

    result = CliRunner().invoke(
        app,
        ["search", "mitochondrion", "--collection", "notes", "--explain", "1"],
    )
    assert result.exit_code == 0
    # Find the JSON block (last balanced { ... } in stdout).
    start = result.stdout.find("{")
    end = result.stdout.rfind("}") + 1
    assert start >= 0
    assert end > start
    trace = json.loads(result.stdout[start:end])
    assert trace["query"] == "mitochondrion"
    assert "regime" in trace
    assert "explained_hit" in trace
    assert trace["explained_hit"]["index"] == 1


def test_search_explain_out_of_range_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--explain N where N > number of hits exits 1 with an error."""
    index_dir, cfg_path = _bootstrap_index(tmp_path)
    monkeypatch.setattr("fnd.cli.default_index_dir", lambda: index_dir)
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    monkeypatch.setattr("fnd.migrate.prompt_and_rebuild_or_exit", lambda **kw: None)

    result = CliRunner().invoke(
        app,
        ["search", "mitochondrion", "--collection", "notes", "--explain", "999"],
    )
    assert result.exit_code == 1
    assert "out of range" in (result.stderr or result.stdout)

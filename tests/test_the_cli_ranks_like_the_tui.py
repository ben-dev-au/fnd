"""`fnd search` runs the TUI's search: synonyms, typed filter flags, and the
rows that `--explain` explains."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fnd.cli import app
from fnd.index import build_index


@pytest.fixture
def cli_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "login.md").write_text("# Login\n\nWe use single sign-on for every app.\n")
    (notes / "cells.md").write_text(
        "# Cells\n\nthe cell membrane and the cell wall.\n\n## Plants\n\na plant cell wall.\n"
    )
    (notes / "walls.txt").write_text("a cell wall, then a membrane further on.\n")
    index_dir = tmp_path / "idx"
    index_dir.mkdir()
    build_index(roots=[notes], index_dir=index_dir, collection="notes")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.notes.sources]]
            path = "{notes.as_posix()}"
        """)
    )
    monkeypatch.setattr("fnd.cli.default_index_dir", lambda: index_dir)
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    monkeypatch.setattr("fnd.migrate.prompt_and_rebuild_or_exit", lambda **kw: None)
    return index_dir


def _search(*args: str) -> str:
    result = CliRunner().invoke(app, ["search", *args])
    assert result.exit_code == 0, result.output
    return result.stdout


def test_a_synonym_only_file_is_found(cli_index: Path) -> None:
    """``sso`` reaches "single sign-on" through the bundled synonym table."""
    assert "login.md" in _search("sso")


def test_explain_explains_the_rows_plain_search_prints(cli_index: Path) -> None:
    """One row per file in both, so row N is the hit ``--explain N`` describes."""
    plain = _search("cell wall")
    explained = _search("cell wall", "--explain", "1")
    rows, _, trace = explained.partition("\n{")
    assert rows + "\n" == plain
    assert json.loads("{" + trace)["explained_hit"]["index"] == 1


def test_a_kind_flag_keeps_the_phrase_pass(cli_index: Path) -> None:
    """As query text, ``kind:md`` made fusion stand its phrase pass down."""
    out = _search("cell wall", "--kind", "md", "--explain", "1")
    trace = json.loads("{" + out.partition("\n{")[2])
    assert "phrase" in {s["source"] for s in trace["fusion"]["subqueries"]}
    assert "walls.txt" not in out.partition("\n{")[0]

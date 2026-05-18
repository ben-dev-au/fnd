"""Phase 5.5e-1: `fnd config validate` reports filter syntax errors."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fnd.cli import app


def _runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str) -> tuple[CliRunner, Path]:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(textwrap.dedent(body), encoding="utf-8")
    monkeypatch.setattr("fnd.cli.default_config_path", lambda: cfg_path)
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return CliRunner(), cfg_path


def test_validate_passes_for_valid_filter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner, _ = _runner(
        monkeypatch,
        tmp_path,
        """
        [[collections.x.sources]]
        path = "~/x"
        frontmatter_filter = "Course == 'DPwC'"
    """,
    )
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 0, result.output
    assert "valid" in result.output.lower()


def test_validate_reports_filter_syntax_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner, _ = _runner(
        monkeypatch,
        tmp_path,
        """
        [[collections.x.sources]]
        path = "~/x"
        frontmatter_filter = "Course =="
    """,
    )
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 1
    assert "frontmatter_filter" in result.output
    assert "col" in result.output

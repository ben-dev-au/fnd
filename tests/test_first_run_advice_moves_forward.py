"""The advice on an empty index never names the command that printed it.

`fnd tui` answering with "run `fnd tui` and choose Add Collection" cost 102
keystrokes and three refusals before a new user saw a screen. Both branches of
`_next_step`'s `if` must move forward, not only one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fnd.cli import app
from fnd.config import CollectionConfig, Config, SourceConfig
from fnd.migrate import _next_step

runner = CliRunner()


def test_the_tui_does_not_advise_running_the_tui() -> None:
    said = _next_step(Config(), invoked="tui")
    assert "fnd collection add" in said
    assert not re.search(r"run `fnd tui`(?! again)", said), said


def test_another_command_may_still_point_at_the_tui() -> None:
    """The control: from `fnd search`, the wizard is good advice."""
    said = _next_step(Config())
    assert "fnd tui" in said
    assert "Add Collection" in said


def test_a_configured_collection_is_told_to_build_it(tmp_path: Path) -> None:
    """The control this function already had: never advise a step taken."""
    config = Config(collections={"notes": CollectionConfig(sources=[SourceConfig(path=tmp_path)])})
    for invoked in ("", "tui"):
        said = _next_step(config, invoked=invoked)
        assert "reindex notes" in said, said
        assert "collection add" not in said


def test_the_launch_path_carries_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end, so removing the argument at the call site fails here."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[defaults]\n", encoding="utf-8")
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    monkeypatch.setattr("fnd.cli.default_index_dir", lambda: tmp_path / "idx")

    result = runner.invoke(app, ["tui"])

    assert result.exit_code == 1
    assert "fnd collection add" in result.output
    assert not re.search(r"run `fnd tui`(?! again)", result.output), result.output

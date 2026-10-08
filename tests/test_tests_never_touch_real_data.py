"""No test reaches the developer's real fnd folders, and nothing replaces a config."""

from __future__ import annotations

from pathlib import Path

import platformdirs
import pytest

from fnd import paths


@pytest.mark.parametrize(
    ("ours", "real"),
    [
        (paths.app_data_dir, platformdirs.user_data_dir),
        (paths.app_cache_dir, platformdirs.user_cache_dir),
    ],
)
def test_every_root_is_a_temp_dir(ours: object, real: object) -> None:
    """In this process; a spawned child is covered by the XDG and Windows overrides."""
    mine = Path(ours())  # pyright: ignore[reportCallIssue, reportArgumentType]
    theirs = Path(real("fnd", appauthor=False))  # pyright: ignore[reportCallIssue]
    assert mine != theirs
    assert not mine.is_relative_to(theirs)


def test_config_edit_never_replaces_a_config(
    isolated_app_dirs: Path, isolated_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from fnd import launcher
    from fnd.cli import app

    # Checked before the write: if the isolation ever lapsed, this must not
    # be the developer's own config.
    assert paths.app_data_dir() == isolated_app_dirs / "data"
    kept = paths.app_data_dir() / "config.toml"
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_text("# mine\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "edit", lambda _path: 0)
    CliRunner().invoke(app, ["config", "edit"])
    assert kept.read_text(encoding="utf-8") == "# mine\n"
    assert isolated_config_path.exists(), "the starter goes where the config is looked for"


def test_a_spawned_interpreter_sees_temp_dirs_too(isolated_app_dirs: Path) -> None:
    import subprocess
    import sys

    probe = "from fnd import paths; print(paths.app_data_dir()); print(paths.app_cache_dir())"
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout.split("\n")
    for line in filter(None, out):
        assert Path(line).is_relative_to(isolated_app_dirs), line

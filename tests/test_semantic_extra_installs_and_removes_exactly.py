"""Installing semantic search fetches the model first and removes exactly what it added."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fnd import config as conf
from fnd import extras, semantic
from fnd.cli import app
from fnd.extras import SEMANTIC, install_commands, set_default_group, uninstall_commands

PY = sys.executable


def _standalone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extras, "_project_pyproject_for_python", lambda py: None)
    monkeypatch.setattr(extras, "_fnd_tool_venv", lambda: False)


def test_the_model_is_fetched_before_any_package(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first install command fetches and verifies the model."""
    _standalone(monkeypatch)
    assert install_commands(SEMANTIC)[0] == [PY, "-m", "fnd.semantic", "fetch"]


def test_standalone_installs_record_what_they_add(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain venv install pins the packages between a before and an after record."""
    _standalone(monkeypatch)
    cmds = install_commands(SEMANTIC)
    record = [PY, "-m", "fnd.extras", "record", "semantic"]
    pip = [c for c in cmds if c[:3] == ["uv", "pip", "install"]]
    assert cmds.index([*record, "before"]) < cmds.index(pip[0]) < cmds.index([*record, "after"])
    assert "onnxruntime~=1.25.1" in pip[0]
    assert "tokenizers~=0.23.3" in pip[0]


def test_record_writes_the_difference(monkeypatch: pytest.MonkeyPatch) -> None:
    """The after record keeps only distributions the install added."""
    names = iter([{"rich", "fndr"}, {"rich", "fndr", "onnxruntime", "numpy"}])
    monkeypatch.setattr(extras, "_installed_names", lambda: next(names))
    extras.record("semantic", "before")
    extras.record("semantic", "after")
    assert extras.recorded_additions("semantic") == {"onnxruntime", "numpy"}


def test_uninstall_removes_only_what_install_added(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recorded additions, less anything fnd itself requires, are uninstalled."""
    _standalone(monkeypatch)
    monkeypatch.setattr(extras, "recorded_additions", lambda name: {"onnxruntime", "numpy", "rich"})
    monkeypatch.setattr(extras, "is_package_installed", lambda pkg: True)
    (cmd,) = uninstall_commands(SEMANTIC)
    assert cmd[:5] == ["uv", "pip", "uninstall", "--python", PY]
    assert sorted(cmd[5:]) == ["numpy", "onnxruntime"]


def test_uv_tool_installs_reinstall_with_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """A uv-tool fnd reinstalls itself with the extra, so upgrades keep it."""
    monkeypatch.setattr(extras, "_project_pyproject_for_python", lambda py: None)
    monkeypatch.setattr(extras, "_fnd_tool_venv", lambda: True)
    monkeypatch.setattr(extras, "_fnd_version", lambda: "1.0.1")
    monkeypatch.setattr(extras, "is_package_installed", lambda pkg: True)
    tool = [c[:5] for c in install_commands(SEMANTIC) if c[:3] == ["uv", "tool", "install"]]
    assert tool == [["uv", "tool", "install", "--reinstall", "fndr[semantic]==1.0.1"]]
    removed = [c[:5] for c in uninstall_commands(SEMANTIC)]
    assert removed == [["uv", "tool", "install", "--reinstall", "fndr==1.0.1"]]


def test_unsupported_platforms_refuse_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Where onnxruntime has no wheel, install explains and runs nothing."""
    monkeypatch.setattr(semantic, "platform_supported", lambda *a: False)
    ran: list[list[str]] = []
    monkeypatch.setattr(extras, "run_command", lambda argv: ran.append(argv) or (0, "", ""))
    result = CliRunner().invoke(app, ["extras", "install", "semantic", "--yes"])
    assert result.exit_code == 2
    assert "not available on this machine" in result.output
    assert ran == []


def test_install_turns_semantic_search_on_and_uninstall_off(
    monkeypatch: pytest.MonkeyPatch, isolated_config_path: Path
) -> None:
    """Install sets semantic_search on; uninstall sets it off and removes model and vectors."""
    _standalone(monkeypatch)
    monkeypatch.setattr(semantic, "platform_supported", lambda *a: True)
    monkeypatch.setattr(extras, "run_command", lambda argv: (0, "", ""))
    monkeypatch.setattr("fnd.cli._require_uv", lambda: None)
    result = CliRunner().invoke(app, ["extras", "install", "semantic", "--yes"])
    assert result.exit_code == 0, result.output
    assert conf.load(isolated_config_path).defaults.semantic_search is True

    for folder in SEMANTIC.cache_dirs():
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "x").write_text("x")
    monkeypatch.setattr(extras, "is_package_installed", lambda pkg: True)
    monkeypatch.setattr(extras, "recorded_additions", lambda name: {"onnxruntime"})
    result = CliRunner().invoke(app, ["extras", "uninstall", "semantic", "--yes"])
    assert result.exit_code == 0, result.output
    assert conf.load(isolated_config_path).defaults.semantic_search is False
    assert not any(f.exists() for f in SEMANTIC.cache_dirs())


def test_the_pdf_structure_toggle_still_works(tmp_path: Path) -> None:
    """set_default_group adds and removes a group from uv's default groups."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[tool.uv]\ndefault-groups = ["dev"]\n')
    set_default_group(pyproject, "pdf-structure", present=True)
    assert "pdf-structure" in pyproject.read_text()
    set_default_group(pyproject, "pdf-structure", present=False)
    assert "pdf-structure" not in pyproject.read_text()


def test_record_survives_a_json_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """The recorded additions are plain JSON in fnd's data folder."""
    names = iter([set(), {"onnxruntime"}])
    monkeypatch.setattr(extras, "_installed_names", lambda: next(names))
    extras.record("semantic", "before")
    extras.record("semantic", "after")
    saved = json.loads(extras._record_path("semantic", "added").read_text())
    assert saved == ["onnxruntime"]


def test_a_uv_tool_reinstall_keeps_other_extras(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reinstalling fnd as a uv tool carries an installed pdf-structure along."""
    monkeypatch.setattr(extras, "_project_pyproject_for_python", lambda py: None)
    monkeypatch.setattr(extras, "_fnd_tool_venv", lambda: True)
    monkeypatch.setattr(extras, "_fnd_version", lambda: "1.0.1")
    monkeypatch.setattr(extras, "is_package_installed", lambda pkg: True)
    (install,) = [c for c in install_commands(SEMANTIC) if c[:3] == ["uv", "tool", "install"]]
    (remove,) = uninstall_commands(SEMANTIC)
    for cmd in (install, remove):
        assert cmd[cmd.index("--with") + 1] == "pymupdf4llm"


def test_a_second_install_keeps_what_the_first_added(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reinstalling over packages already present still remembers them for uninstall."""
    names = iter(
        [{"fndr"}, {"fndr", "onnxruntime"}, {"fndr", "onnxruntime"}, {"fndr", "onnxruntime"}]
    )
    monkeypatch.setattr(extras, "_installed_names", lambda: next(names))
    for phase in ("before", "after", "before", "after"):
        extras.record("semantic", phase)
    assert extras.recorded_additions("semantic") == {"onnxruntime"}

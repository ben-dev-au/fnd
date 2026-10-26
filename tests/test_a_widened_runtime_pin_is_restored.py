"""`check_runtime_pins.py --fix` turns a Dependabot range back into `~=` the locked version."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "check_runtime_pins", _ROOT / ".github" / "scripts" / "check_runtime_pins.py"
)
assert _SPEC is not None
assert _SPEC.loader is not None
check_runtime_pins = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_runtime_pins)

_PYPROJECT = """\
[project]
name = "demo"
dependencies = [
    "tantivy>=0.26.2,<0.28.0",
    "Tomli_W>=1.2,<1.4",
    "termaid[rich]>=0.9,<0.11",
    "rich~=15.0.0",
]

[project.optional-dependencies]
semantic = ["onnxruntime>=1.31.0,<1.33"]
cli = ["typer>=0.27,<0.29"]

[dependency-groups]
pdf-structure = ["pymupdf4llm>=1.28.2,<1.30"]
semantic = ["onnxruntime>=1.31.0,<1.33"]
dev = ["ruff>=0.17.0"]
"""

_LOCK = """\
version = 1
revision = 5

[[package]]
name = "tantivy"
version = "0.27.0"

[[package]]
name = "tomli-w"
version = "1.3"

[[package]]
name = "termaid"
version = "0.10.1"

[[package]]
name = "rich"
version = "15.0.4"

[[package]]
name = "pymupdf4llm"
version = "1.29.0.post1"

[[package]]
name = "onnxruntime"
version = "1.32.1"

[[package]]
name = "typer"
version = "0.28.2"
"""


def _project(tmp_path: Path, pyproject: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    (tmp_path / "uv.lock").write_text(_LOCK, encoding="utf-8")
    return tmp_path


def test_every_widened_pin_returns_to_the_locked_minor(tmp_path: Path) -> None:
    root = _project(tmp_path, _PYPROJECT)

    changes = check_runtime_pins.fix(root)

    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert '"tantivy~=0.27.0"' in text
    assert '"Tomli_W~=1.3.0"' in text
    assert '"termaid[rich]~=0.10.1"' in text
    assert '"pymupdf4llm~=1.29.0"' in text
    assert text.count('"onnxruntime~=1.32.1"') == 2
    assert '"typer~=0.28.2"' in text
    assert '"rich~=15.0.0"' in text
    assert '"ruff>=0.17.0"' in text
    assert len(changes) == 6


def test_a_pinned_project_is_left_byte_identical(tmp_path: Path) -> None:
    pinned = _PYPROJECT.replace("tantivy>=0.26.2,<0.28.0", "tantivy~=0.27.0")
    pinned = pinned.replace("Tomli_W>=1.2,<1.4", "Tomli_W~=1.3.0")
    pinned = pinned.replace("termaid[rich]>=0.9,<0.11", "termaid[rich]~=0.10.1")
    pinned = pinned.replace('["pymupdf4llm>=1.28.2,<1.30"]', '["pymupdf4llm~=1.29.0"]')
    pinned = pinned.replace("onnxruntime>=1.31.0,<1.33", "onnxruntime~=1.32.1")
    pinned = pinned.replace("typer>=0.27,<0.29", "typer~=0.28.2")
    root = _project(tmp_path, pinned)

    assert check_runtime_pins.fix(root) == []
    assert (root / "pyproject.toml").read_text(encoding="utf-8") == pinned


def test_an_unlocked_dependency_refuses_rather_than_guessing(tmp_path: Path) -> None:
    root = _project(tmp_path, _PYPROJECT.replace("tantivy>=", "notlocked>="))

    with pytest.raises(SystemExit, match="notlocked"):
        check_runtime_pins.fix(root)

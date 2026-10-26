"""`base_vulns.py` ignores exactly the base branch's findings, and nothing when the base audit failed."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "base_vulns", _ROOT / ".github" / "scripts" / "base_vulns.py"
)
assert _SPEC is not None
assert _SPEC.loader is not None
base_vulns = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(base_vulns)

_REPORT = {
    "dependencies": [
        {"name": "filelock", "version": "3.29.0", "vulns": [{"id": "CVE-1"}, {"id": "CVE-2"}]},
        {"name": "rich", "version": "15.0.4", "vulns": []},
        {"name": "fndr", "skip_reason": "not on PyPI"},
    ],
    "fixes": [],
}


def test_every_base_finding_becomes_an_ignore(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "base-audit.json"
    report.write_text(json.dumps(_REPORT), encoding="utf-8")

    assert base_vulns.main(str(report)) == 0

    assert capsys.readouterr().out.split() == ["--ignore-vuln", "CVE-1", "--ignore-vuln", "CVE-2"]


def test_a_missing_base_report_ignores_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert base_vulns.main(str(tmp_path / "absent.json")) == 0

    assert capsys.readouterr().out == ""

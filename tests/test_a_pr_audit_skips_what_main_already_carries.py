"""`new_vulns.py` fails a PR only on a package and advisory pair the base branch lacks."""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "new_vulns", _ROOT / ".github" / "scripts" / "new_vulns.py"
)
assert _SPEC is not None
assert _SPEC.loader is not None
new_vulns = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(new_vulns)


def _report(path: Path, *deps: tuple[str, str, Sequence[Mapping[str, object]]]) -> str:
    body = [{"name": n, "version": v, "vulns": vulns} for n, v, vulns in deps]
    body.append({"name": "fndr", "skip_reason": "not on PyPI"})
    path.write_text(json.dumps({"dependencies": body, "fixes": []}), encoding="utf-8")
    return str(path)


_FILELOCK = ("filelock", "3.29.0", [{"id": "CVE-1", "aliases": ["GHSA-a"]}])


def test_a_finding_the_base_carries_passes(tmp_path: Path) -> None:
    base = _report(tmp_path / "base.json", _FILELOCK)
    renamed = ("FileLock", "3.29.1", [{"id": "GHSA-a", "aliases": []}])
    pr = _report(tmp_path / "pr.json", renamed)

    assert new_vulns.main(base, pr) == 0


def test_a_new_finding_fails(tmp_path: Path) -> None:
    base = _report(tmp_path / "base.json", _FILELOCK)
    pr = _report(tmp_path / "pr.json", _FILELOCK, ("virtualenv", "21.7.13", [{"id": "CVE-2"}]))

    assert new_vulns.main(base, pr) == 1


def test_a_shared_advisory_on_another_package_fails(tmp_path: Path) -> None:
    base = _report(tmp_path / "base.json", _FILELOCK)
    pr = _report(tmp_path / "pr.json", _FILELOCK, ("py-filelock", "1.0", [{"id": "CVE-1"}]))

    assert new_vulns.main(base, pr) == 1


def test_a_missing_base_report_audits_strictly(tmp_path: Path) -> None:
    pr = _report(tmp_path / "pr.json", _FILELOCK)

    assert new_vulns.main(str(tmp_path / "absent.json"), pr) == 1


def test_a_missing_pr_report_fails(tmp_path: Path) -> None:
    base = _report(tmp_path / "base.json")

    assert new_vulns.main(base, str(tmp_path / "absent.json")) == 1

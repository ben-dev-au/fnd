"""Print `--ignore-vuln ID` for every vulnerability a base-branch pip-audit JSON report found.

A PR audit then fails only on what the PR introduces; the push and cron audits on
main stay strict, so an advisory against an untouched dependency still surfaces there.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def base_vulns(report: dict[str, object]) -> list[tuple[str, str, str]]:
    """Return (package, version, id) for every vulnerability in a pip-audit JSON report."""
    found: list[tuple[str, str, str]] = []
    deps = report.get("dependencies", [])
    assert isinstance(deps, list)
    for dep in deps:
        assert isinstance(dep, dict)
        for vuln in dep.get("vulns", []):
            found.append((dep["name"], dep["version"], vuln["id"]))
    return found


def main(path: str) -> int:
    report = Path(path)
    # No report means the base audit errored; ignoring nothing keeps the PR audit strict.
    if not report.is_file():
        print(f"::warning::no base audit at {path}; auditing the PR strictly", file=sys.stderr)
        return 0

    for package, version, vuln_id in base_vulns(json.loads(report.read_text(encoding="utf-8"))):
        print(
            f"::warning::{package} {version} {vuln_id} is already on the base branch",
            file=sys.stderr,
        )
        print("--ignore-vuln")
        print(vuln_id)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))

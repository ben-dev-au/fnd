"""Fail on the vulnerabilities a PR's pip-audit JSON report has that the base branch's lacks.

A finding is inherited only when the base reports the same package with an advisory
sharing an ID or alias. Push and cron audits on main stay strict, so inherited
findings still surface there.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

Finding = tuple[str, str, frozenset[str]]


def findings(report: dict[str, object]) -> list[Finding]:
    """Return (package, version, advisory IDs and aliases) for every vulnerability in a report."""
    found: list[Finding] = []
    deps = report.get("dependencies", [])
    assert isinstance(deps, list)
    for dep in deps:
        assert isinstance(dep, dict)
        name = re.sub(r"[-_.]+", "-", str(dep["name"])).lower()
        for vuln in dep.get("vulns", []):
            found.append(
                (name, str(dep["version"]), frozenset([vuln["id"], *vuln.get("aliases", [])]))
            )
    return found


def introduced(base: list[Finding], pr: list[Finding]) -> list[Finding]:
    """Return the PR findings no base finding matches by package and a shared advisory ID."""
    return [
        (package, version, ids)
        for package, version, ids in pr
        if not any(package == b_package and ids & b_ids for b_package, _, b_ids in base)
    ]


def _load(path: Path) -> list[Finding] | None:
    if not path.is_file():
        return None
    return findings(json.loads(path.read_text(encoding="utf-8")))


def main(base_path: str, pr_path: str) -> int:
    pr = _load(Path(pr_path))
    if pr is None:
        print(f"::error::the PR audit wrote no report at {pr_path}; see the step log")
        return 1
    # No base report means the base audit errored; treating it as clean keeps the PR strict.
    base = _load(Path(base_path))
    if base is None:
        print(f"::warning::no base audit at {base_path}; auditing the PR strictly")
        base = []

    new = introduced(base, pr)
    for package, version, ids in pr:
        level = "error" if (package, version, ids) in new else "warning"
        note = "introduced by this PR" if level == "error" else "already on the base branch"
        print(f"::{level}::{package} {version} {', '.join(sorted(ids))}: {note}")

    print(f"{len(new)} new, {len(pr) - len(new)} inherited")
    return 1 if new else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))

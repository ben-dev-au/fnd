"""Every user-facing dependency pins the minor: `~=X.Y.Z`, never `~=X.Y` or a range."""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

# Three-component compatible-release, extras allowed: `termaid[rich]~=0.6.1`.
PIN = re.compile(r"^[A-Za-z0-9._-]+(\[[A-Za-z0-9,._-]+\])?~=\d+\.\d+\.\d+$")

# `dev` never reaches a user install; every other group here does.
USER_FACING_GROUPS = ("pdf-structure",)


def offenders(pyproject: dict[str, object]) -> list[tuple[str, str]]:
    """Return (location, specifier) for every user-facing dep that is not pinned."""
    project = pyproject["project"]
    assert isinstance(project, dict)
    checked = [("project.dependencies", d) for d in project["dependencies"]]

    groups = pyproject.get("dependency-groups", {})
    assert isinstance(groups, dict)
    for name in USER_FACING_GROUPS:
        checked += [(f"dependency-groups.{name}", d) for d in groups.get(name, [])]

    return [(where, spec) for where, spec in checked if not PIN.match(spec)]


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    bad = offenders(pyproject)

    for where, spec in bad:
        print(f"::error::{where}: {spec!r} is not pinned to the minor (`~=X.Y.Z`)")

    if bad:
        print(
            f"\n{len(bad)} user-facing dependency specifier(s) are not pinned.\n"
            "uv.lock reaches no installer, so these specifiers are what every user\n"
            "resolves. A widened range ships versions CI never ran.\n"
            "Dependabot widens by default — correct the specifier before merging."
        )
        return 1

    print("all user-facing dependencies pinned `~=X.Y.Z`")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Every user-facing dependency pins the minor: `~=X.Y.Z`, never `~=X.Y` or a range.

`--fix` rewrites each offender to `~=` the version uv.lock resolves, which is how a
Dependabot PR (its uv ecosystem always widens to a range) gets its pin back.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

# Three-component compatible-release, extras allowed: `termaid[rich]~=0.6.1`.
PIN = re.compile(r"^[A-Za-z0-9._-]+(\[[A-Za-z0-9,._-]+\])?~=\d+\.\d+\.\d+$")
NAME = re.compile(r"^([A-Za-z0-9._-]+)(\[[A-Za-z0-9,._-]+\])?")
RELEASE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")

# `dev` never reaches a user install; these groups and every extra do.
USER_FACING_GROUPS = ("pdf-structure", "semantic")


def offenders(pyproject: dict[str, object]) -> list[tuple[str, str]]:
    """Return (location, specifier) for every user-facing dep that is not pinned."""
    project = pyproject["project"]
    assert isinstance(project, dict)
    checked = [("project.dependencies", d) for d in project["dependencies"]]

    extras = project.get("optional-dependencies", {})
    assert isinstance(extras, dict)
    for name, deps in extras.items():
        checked += [(f"project.optional-dependencies.{name}", d) for d in deps]

    groups = pyproject.get("dependency-groups", {})
    assert isinstance(groups, dict)
    for name in USER_FACING_GROUPS:
        checked += [(f"dependency-groups.{name}", d) for d in groups.get(name, [])]

    return [(where, spec) for where, spec in checked if not PIN.match(spec)]


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pinned(spec: str, lock: dict[str, object]) -> str:
    """Return `spec` rewritten as `name[extras]~=X.Y.Z` at the version uv.lock resolves."""
    match = NAME.match(spec)
    if match is None:
        raise SystemExit(f"::error::cannot read a package name from {spec!r}")
    name, extras = match.group(1), match.group(2) or ""

    packages = lock.get("package", [])
    assert isinstance(packages, list)
    locked = next(
        (
            p["version"]
            for p in packages
            if isinstance(p, dict) and _normalise(str(p.get("name"))) == _normalise(name)
        ),
        None,
    )
    release = RELEASE.match(str(locked)) if locked is not None else None
    if release is None:
        raise SystemExit(f"::error::uv.lock resolves no release version for {name}")

    major, minor, micro = release.group(1), release.group(2), release.group(3) or "0"
    return f"{name}{extras}~={major}.{minor}.{micro}"


def fix(root: Path) -> list[tuple[str, str]]:
    """Rewrite every offender in pyproject.toml in place; return (old, new) per change."""
    path = root / "pyproject.toml"
    text = path.read_text(encoding="utf-8")
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))

    # An extra mirrored as a group holds the same spec twice; both copies move together.
    specs = dict.fromkeys(spec for _, spec in offenders(tomllib.loads(text)))
    changes = [(spec, pinned(spec, lock)) for spec in specs]
    for old, new in changes:
        quoted = f'"{old}"'
        if quoted not in text:
            raise SystemExit(f"::error::{quoted} is not written that way in pyproject.toml")
        text = text.replace(quoted, f'"{new}"')

    path.write_text(text, encoding="utf-8")
    return changes


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parents[2]

    if argv == ["--fix"]:
        for old, new in fix(root):
            print(f"{old}  ->  {new}")

    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    bad = offenders(pyproject)

    for where, spec in bad:
        print(f"::error::{where}: {spec!r} is not pinned to the minor (`~=X.Y.Z`)")

    if bad:
        print(
            f"\n{len(bad)} user-facing dependency specifier(s) are not pinned.\n"
            "uv.lock reaches no installer, so these specifiers are what every user\n"
            "resolves. A widened range ships versions CI never ran.\n"
            "Dependabot widens by default: `dependabot-pins.yml` restores the pin,\n"
            "or run this script with `--fix` and then `uv lock`."
        )
        return 1

    print("all user-facing dependencies pinned `~=X.Y.Z`")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

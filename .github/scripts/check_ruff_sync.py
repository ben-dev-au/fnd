"""The ruff-pre-commit rev must match the ruff version uv.lock resolves."""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

REV = re.compile(
    r"-\s+repo:\s*https://github\.com/astral-sh/ruff-pre-commit.*?^\s*rev:\s*v?([\d.]+)",
    re.S | re.M,
)


def versions(root: Path) -> tuple[str | None, str | None]:
    """Return (pre-commit rev, uv.lock version) for ruff."""
    config = (root / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    match = REV.search(config)

    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    packages = lock.get("package", [])
    assert isinstance(packages, list)
    locked = next(
        (p["version"] for p in packages if isinstance(p, dict) and p.get("name") == "ruff"),
        None,
    )
    return (match.group(1) if match else None, locked)


def main() -> int:
    hook, locked = versions(Path(__file__).resolve().parents[2])

    if hook is None or locked is None:
        print("::error::could not read the ruff version from both files")
        return 1

    if hook != locked:
        print(
            f"::error::ruff drift — .pre-commit-config.yaml pins v{hook}, uv.lock resolves {locked}"
        )
        print(
            "\nThe hook and CI must run the same ruff. Drift means the hook "
            "reformats\nfiles that `uv run ruff format --check .` then rejects, or "
            "crashes on code\nCI accepts. Bump `rev:` to match in the same PR as the "
            "lock change."
        )
        return 1

    print(f"ruff in sync: v{locked}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

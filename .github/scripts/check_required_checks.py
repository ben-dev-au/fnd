"""Branch protection must require exactly the pytest jobs the matrix produces."""

from __future__ import annotations

import itertools
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = "ben-dev-au/fnd"


def matrix_job_names(ci_yaml: Path) -> set[str]:
    """The pytest check names ci.yml will report, derived from its own matrix."""
    # Stdlib only, so the `pins` job stays a 6-second checkout-and-run.
    text = ci_yaml.read_text(encoding="utf-8")
    block = text[text.index("\n  test:") : text.index("\n  format:")]

    def find(pattern: str, what: str) -> str:
        match = re.search(pattern, block, re.M)
        if match is None:
            raise SystemExit(f"::error::could not read the {what} from ci.yml")
        return match.group(1).strip()

    template = find(r"^    name: (.+)$", "test job name")
    oses = find(r"^        os: \[(.+)\]$", "matrix os list")
    shards = find(r"^        shard: \[(.+)\]$", "matrix shard list")

    names: set[str] = set()
    for os_name, shard in itertools.product(
        (o.strip() for o in oses.split(",")), (s.strip() for s in shards.split(","))
    ):
        name = template.replace("${{ matrix.os }}", os_name)
        names.add(name.replace("${{ matrix.shard }}", shard))
    return names


def required_pytest_checks() -> set[str]:
    """The pytest contexts branch protection currently requires."""
    out = subprocess.run(
        ["gh", "api", f"repos/{REPO}/branches/main/protection/required_status_checks"],
        capture_output=True,
        text=True,
        check=True,
    )
    contexts: list[str] = json.loads(out.stdout)["contexts"]
    return {c for c in contexts if c.startswith("pytest")}


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    produced = matrix_job_names(root / ".github" / "workflows" / "ci.yml")

    try:
        required = required_pytest_checks()
    except subprocess.CalledProcessError:
        # A fork has no token for the protection API; skip rather than fail
        # a contributor's PR for a permission they cannot have.
        print("::notice::branch protection unreadable here; skipped the comparison")
        return 0

    missing = produced - required
    stale = required - produced

    for name in sorted(missing):
        print(f"::error::matrix produces {name!r} but branch protection does not require it")
    for name in sorted(stale):
        print(f"::error::branch protection requires {name!r} but no matrix job reports it")

    if missing or stale:
        print(
            "\nA required check no job reports blocks every PR forever; a job no\n"
            "rule requires can go red without blocking anything. Update the\n"
            "required checks in the same change as the matrix."
        )
        return 1

    print(f"branch protection matches the matrix ({len(produced)} pytest checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

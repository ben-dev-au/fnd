"""Print the test files belonging to one shard; shards partition the set exactly."""

from __future__ import annotations

import sys
from pathlib import Path


def all_test_files(root: Path) -> list[str]:
    """Every collectable test module, in a stable order."""
    # Relative to the repo root: absolute paths break word-splitting wherever
    # the checkout has a space in it, and pytest then collects nothing.
    return sorted(p.relative_to(root).as_posix() for p in (root / "tests").rglob("test_*.py"))


def shard(files: list[str], index: int, total: int) -> list[str]:
    """Deal files round-robin so every file lands in exactly one shard."""
    if not 1 <= index <= total:
        raise ValueError(f"shard {index} outside 1..{total}")
    return [f for i, f in enumerate(files) if i % total == index % total]


def main() -> int:
    index, total = int(sys.argv[1]), int(sys.argv[2])
    files = all_test_files(Path(__file__).resolve().parents[2])

    if not files:
        print("::error::no test files found", file=sys.stderr)
        return 1

    mine = shard(files, index, total)
    if not mine:
        print(f"::error::shard {index}/{total} is empty", file=sys.stderr)
        return 1

    print("\n".join(mine))
    return 0


if __name__ == "__main__":
    sys.exit(main())

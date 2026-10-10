"""Skip a pre-push check on a tree ``make batch-close`` has already passed.

``batch-close`` notes the tree it starts on (``start``): the tree a commit of
every tracked and unignored file would hold. Once pyright, the whole suite and
the harness have passed, ``record`` keeps that tree, unless a file changed
during the run. The pre-push hooks wrap their command in ``check``, which skips
when the pushed commit's tree (pre-commit's ``PRE_COMMIT_TO_REF``) was recorded
and runs the command otherwise. CI runs everything regardless.

The record lives in the git common dir, shared by every worktree and never
committed; the start tree lives in the worktree's own git dir, one file per
gate run (``RUN`` is make's pid), so two gates cannot swap trees.

Usage: ``proven_trees.py start RUN`` | ``record RUN`` | ``check -- COMMAND...``
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_KEPT = 200
_STALE_SECONDS = 86_400


def _git(*args: str, env: dict[str, str] | None = None) -> str:
    done = subprocess.run(["git", *args], check=True, capture_output=True, text=True, env=env)
    return done.stdout.strip()


def _store() -> Path:
    common = _git("rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(common) / "fnd-proven-trees"


def _git_dir() -> Path:
    return Path(_git("rev-parse", "--path-format=absolute", "--git-dir"))


def _recorded() -> list[str]:
    try:
        return _store().read_text(encoding="utf-8").split()
    except FileNotFoundError:
        return []


def working_tree() -> str:
    """The tree a commit of every tracked and unignored file here would hold."""
    # Inside the git dir, so the index never lands in the tree it describes.
    with tempfile.TemporaryDirectory(dir=_git_dir()) as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        _git("read-tree", "HEAD", env=env)
        _git("add", "--all", env=env)
        return _git("write-tree", env=env)


def _start_marker(run: str) -> Path:
    return _git_dir() / f"fnd-gate-start-{run}"


def start(run: str) -> str:
    """Note this run's tree, and drop the marker of any run aborted a day ago."""
    for stale in _git_dir().glob("fnd-gate-start-*"):
        with contextlib.suppress(FileNotFoundError):  # another run's record took it
            if time.time() - stale.stat().st_mtime > _STALE_SECONDS:
                stale.unlink()
    tree = working_tree()
    _start_marker(run).write_text(tree, encoding="utf-8")
    return tree


def record(run: str) -> tuple[str | None, str]:
    """``(the tree kept as passed, "")``, or ``(None, why it was not kept)``."""
    marker = _start_marker(run)
    try:
        started = marker.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None, "no start was noted for this run"
    marker.unlink()
    tree = working_tree()
    if tree != started:
        return None, "files changed during the run"
    kept = [t for t in _recorded() if t != tree][-(_KEPT - 1) :]
    _store().write_text("\n".join([*kept, tree]) + "\n", encoding="utf-8")
    return tree, ""


def proven(commit: str) -> bool:
    return _git("rev-parse", f"{commit}^{{tree}}") in _recorded()


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "start":
        start(argv[1])
        return 0
    if len(argv) == 2 and argv[0] == "record":
        tree, why = record(argv[1])
        print(
            f"batch-close: recorded tree {tree[:12]}"
            if tree
            else f"batch-close: {why}; tree not recorded"
        )
        return 0
    if len(argv) > 2 and argv[:2] == ["check", "--"]:
        commit = os.environ.get("PRE_COMMIT_TO_REF", "")
        if commit and proven(commit):
            print(f"{commit[:12]}: make batch-close passed this tree; skipped.")
            return 0
        return subprocess.call(argv[2:])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

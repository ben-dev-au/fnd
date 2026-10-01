"""A git hook's repo environment must not reach the suite's own git calls.

Pushing from a worktree runs the pre-push suite with ``GIT_DIR`` set to
``.git/worktrees/<name>``; a test's ``git init <tmp>`` then re-initialises the
main repository instead and, guessing from that path, marks it ``bare``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

_GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def test_git_init_creates_the_repo_it_names(tmp_path: Path) -> None:
    """The probe the hook-environment run executes."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert (tmp_path / ".git").is_dir()


def test_a_worktree_hooks_git_dir_leaves_the_main_repo_alone(tmp_path: Path) -> None:
    """Under a worktree hook's ``GIT_DIR`` the suite still inits where it says."""
    main = tmp_path / "main"
    subprocess.run([*_GIT, "init", "-q", str(main)], check=True)
    subprocess.run(
        [*_GIT, "-C", str(main), "commit", "-q", "--allow-empty", "-m", "seed"], check=True
    )
    subprocess.run(
        [*_GIT, "-C", str(main), "worktree", "add", "-q", str(tmp_path / "wt")], check=True
    )

    env = {**os.environ, "GIT_DIR": str(main / ".git" / "worktrees" / "wt")}
    probe = f"{__file__}::test_git_init_creates_the_repo_it_names"
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", probe],
        cwd=Path(__file__).parent.parent,
        env=env,
        capture_output=True,
        text=True,
    )

    bare = subprocess.run(
        ["git", "-C", str(main), "config", "core.bare"], capture_output=True, text=True
    ).stdout.strip()
    assert bare == "false", "the hook's GIT_DIR re-initialised the main repo as bare"
    assert run.returncode == 0, run.stdout[-2000:]

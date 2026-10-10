"""A pre-push check skips a commit whose tree ``make batch-close`` recorded, and
runs for any other tree."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "proven_trees.py"

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True, check=False).returncode != 0,
    reason="needs git",
)


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    env = {
        "GIT_CONFIG_GLOBAL": str(tmp_path / "no-global-config"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(tmp_path),
        "PATH": os.environ["PATH"],
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (root / "a.py").write_text("a = 1\n", encoding="utf-8")
    for args in (["init", "-q"], ["add", "--all"], ["commit", "-qm", "one"]):
        subprocess.run(["git", *args], check=True, cwd=root, env=env)
    return root, env


def _run(repo: tuple[Path, dict[str, str]], *args: str, to_ref: str = "") -> str:
    root, env = repo
    env = {**env, "PRE_COMMIT_TO_REF": to_ref} if to_ref else env
    done = subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=root, env=env, capture_output=True, text=True
    )
    return f"{done.returncode}:{done.stdout}"


def _commit(repo: tuple[Path, dict[str, str]], message: str) -> str:
    root, env = repo
    subprocess.run(["git", "add", "--all"], check=True, cwd=root, env=env)
    subprocess.run(["git", "commit", "-qm", message], check=True, cwd=root, env=env)
    done = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, env=env, capture_output=True)
    return done.stdout.decode().strip()


CHECK = ("check", "--", sys.executable, "-c", "print('ran'); raise SystemExit(3)")


def test_a_gated_tree_committed_afterwards_skips_the_check(
    repo: tuple[Path, dict[str, str]],
) -> None:
    root, _ = repo
    (root / "a.py").write_text("a = 2\n", encoding="utf-8")
    (root / "new.py").write_text("b = 1\n", encoding="utf-8")
    (root / "ignored").mkdir()
    (root / "ignored" / "x.py").write_text("noise\n", encoding="utf-8")
    _run(repo, "start", "1")
    _run(repo, "record", "1")
    commit = _commit(repo, "two")
    assert _run(repo, *CHECK, to_ref=commit).startswith("0:")


def test_an_edit_after_the_gate_runs_the_check(repo: tuple[Path, dict[str, str]]) -> None:
    root, _ = repo
    _run(repo, "start", "1")
    _run(repo, "record", "1")
    (root / "a.py").write_text("a = 3\n", encoding="utf-8")
    commit = _commit(repo, "edited after the gate")
    assert _run(repo, *CHECK, to_ref=commit) == "3:ran\n"


def test_without_a_pushed_commit_the_check_runs(repo: tuple[Path, dict[str, str]]) -> None:
    _run(repo, "start", "1")
    _run(repo, "record", "1")
    assert _run(repo, *CHECK) == "3:ran\n"


def test_an_edit_during_the_gate_records_nothing(repo: tuple[Path, dict[str, str]]) -> None:
    root, _ = repo
    _run(repo, "start", "1")
    (root / "a.py").write_text("a = BROKEN(\n", encoding="utf-8")
    assert "not recorded" in _run(repo, "record", "1")
    assert _run(repo, *CHECK, to_ref=_commit(repo, "edited mid-gate")) == "3:ran\n"


def test_a_record_without_a_start_records_nothing(repo: tuple[Path, dict[str, str]]) -> None:
    root, _ = repo
    (root / "a.py").write_text("a = 4\n", encoding="utf-8")
    assert "no start was noted for this run; tree not recorded" in _run(repo, "record", "1")
    assert _run(repo, *CHECK, to_ref=_commit(repo, "no gate")) == "3:ran\n"


def test_two_gates_in_one_worktree_keep_their_own_start(repo: tuple[Path, dict[str, str]]) -> None:
    root, _ = repo
    _run(repo, "start", "1")
    (root / "a.py").write_text("a = BROKEN(\n", encoding="utf-8")
    _run(repo, "start", "2")
    assert "files changed during the run; tree not recorded" in _run(repo, "record", "1")
    assert _run(repo, *CHECK, to_ref=_commit(repo, "edited between two gates")) == "3:ran\n"


def test_a_start_drops_markers_of_long_aborted_runs(repo: tuple[Path, dict[str, str]]) -> None:
    root, _ = repo
    _run(repo, "start", "1")
    old = root / ".git" / "fnd-gate-start-1"
    os.utime(old, (0, 0))
    _run(repo, "start", "2")
    assert not old.exists()
    assert (root / ".git" / "fnd-gate-start-2").exists()

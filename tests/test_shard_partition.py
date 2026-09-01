"""CI shards must partition the test files exactly: no file dropped, none run twice."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "shard_tests", _ROOT / ".github" / "scripts" / "shard_tests.py"
)
assert _SPEC is not None
assert _SPEC.loader is not None
shard_tests = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(shard_tests)


@pytest.mark.parametrize("total", [2, 3, 4, 5, 6])
def test_shards_cover_every_file_exactly_once(total: int) -> None:
    files = shard_tests.all_test_files(_ROOT)
    assert files, "no test files discovered"

    parts = [shard_tests.shard(files, i, total) for i in range(1, total + 1)]
    flat = [f for part in parts for f in part]

    assert sorted(flat) == files, "shards do not reconstruct the full set"
    assert len(flat) == len(set(flat)), "a file appears in more than one shard"
    assert all(parts), "a shard is empty"


def test_shard_index_is_validated() -> None:
    files = shard_tests.all_test_files(_ROOT)
    with pytest.raises(ValueError, match=r"outside 1\.\.3"):
        shard_tests.shard(files, 0, 3)
    with pytest.raises(ValueError, match=r"outside 1\.\.3"):
        shard_tests.shard(files, 4, 3)


def test_stdout_has_no_carriage_returns() -> None:
    # Windows print() emits \r\n; the \r then rides into every path the shell
    # splits out and pytest exits 4 on files that do not exist.
    out = subprocess.run(
        [sys.executable, str(_ROOT / ".github" / "scripts" / "shard_tests.py"), "1", "3"],
        capture_output=True,
        check=True,
    )
    assert b"\r" not in out.stdout
    assert out.stdout.endswith(b"\n")


def test_paths_are_relative_and_space_free() -> None:
    # Absolute paths break shell word-splitting on a checkout containing spaces.
    for f in shard_tests.all_test_files(_ROOT):
        assert not f.startswith("/"), f
        assert " " not in f, f

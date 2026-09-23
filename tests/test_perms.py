"""Restrictive permissions on the fnd app data tree. (M7)

The whole point: another local user on a shared Mac should not be able
to enumerate the documents we've indexed by reading the config TOML or
walking the index directory.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from fnd._perms import secure_mkdir, secure_write_text

# The 0o700/0o600 hardening is POSIX-only; os.chmod merely toggles the read-only
# bit on Windows (the user-profile data dir is already ACL-scoped there). So we
# skip only the mode-bit assertions on Windows — the functional behaviour
# (creation, atomic replace, idempotence) still runs and is asserted, giving
# _perms real Windows coverage. See fnd/_perms.py for the platform note.
_POSIX_MODES = sys.platform != "win32"


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def _expect_mode(p: Path, mode: int) -> None:
    if _POSIX_MODES:
        assert _mode(p) == mode, p


def test_secure_mkdir_chmods_leaf_and_intermediate(tmp_path: Path) -> None:
    anchor = tmp_path / "anchor"
    target = anchor / "a" / "b" / "c"
    secure_mkdir(target, anchor=anchor)
    assert target.is_dir()
    for p in (anchor, anchor / "a", anchor / "a" / "b", target):
        _expect_mode(p, 0o700)


def test_secure_mkdir_outside_anchor_only_chmods_leaf(tmp_path: Path) -> None:
    """When the target isn't a descendant of the anchor, we leave the
    intermediate dirs untouched (they likely belong to someone else)."""
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    elsewhere = tmp_path / "elsewhere" / "deep"
    secure_mkdir(elsewhere, anchor=anchor)
    assert elsewhere.is_dir()
    _expect_mode(elsewhere, 0o700)
    # The anchor wasn't on the path; it should be untouched (default umask).
    if _POSIX_MODES:
        assert _mode(anchor) != 0o700


def test_secure_write_text_sets_0o600(tmp_path: Path) -> None:
    target = tmp_path / "secret.toml"
    secure_write_text(target, "k = 1\n")
    assert target.read_text() == "k = 1\n"
    _expect_mode(target, 0o600)


def test_secure_write_text_atomic_replaces_via_tmp(tmp_path: Path) -> None:
    target = tmp_path / "state.toml"
    target.write_text("old\n", encoding="utf-8")
    secure_write_text(target, "new\n", atomic=True)
    assert target.read_text() == "new\n"
    # Any sibling, not one spelling of it: the name carries a pid and a
    # counter, so asserting `.tmp` alone passed however the write behaved.
    assert [f.name for f in tmp_path.iterdir()] == [target.name]
    _expect_mode(target, 0o600)


def test_secure_write_text_atomic_leaves_the_old_file_on_failure(tmp_path: Path) -> None:
    """The point of the temp file: a failed write must not truncate what is
    already there, and must not litter the directory."""
    import fnd._perms as perms

    target = tmp_path / "state.toml"
    target.write_text("old\n", encoding="utf-8")
    broken = perms.os.replace

    def fail(*_args: object, **_kw: object) -> None:
        raise OSError("no space left on device")

    perms.os.replace = fail
    try:
        with pytest.raises(OSError, match="no space"):
            secure_write_text(target, "new\n", atomic=True)
    finally:
        perms.os.replace = broken
    assert target.read_text() == "old\n", "the previous config was lost"
    assert [f.name for f in tmp_path.iterdir()] == [target.name], "temp file left behind"


def test_secure_mkdir_idempotent(tmp_path: Path) -> None:
    anchor = tmp_path / "anchor"
    target = anchor / "sub"
    secure_mkdir(target, anchor=anchor)
    # Pre-tamper with the perms; secure_mkdir should restore them.
    os.chmod(target, 0o755)
    secure_mkdir(target, anchor=anchor)
    assert target.is_dir()
    _expect_mode(target, 0o700)

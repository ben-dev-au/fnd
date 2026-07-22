"""Filesystem timestamp reads, including the non-Darwin degradation."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from fnd.fsmeta import FileTimes, read_file_times


def test_reads_all_three_timestamps(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    times = read_file_times(f)
    assert times.mtime > 0
    assert times.inode_changed > 0


@pytest.mark.skipif(sys.platform != "darwin", reason="birthtime is Darwin-only")
def test_created_is_populated_on_darwin(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    assert read_file_times(f).created > 0


def test_missing_birthtime_degrades_to_zero_on_linux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linux stat() has no st_birthtime and no Windows ctime-as-created;
    created must be 0, not an error."""
    monkeypatch.setattr(sys, "platform", "linux")
    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    fake = types.SimpleNamespace(st_mtime=1000.0, st_ctime=2000.0)
    monkeypatch.setattr(Path, "stat", lambda self, **kw: fake)
    times = read_file_times(f)
    assert times == FileTimes(mtime=1000, created=0, inode_changed=2000)


def test_windows_uses_ctime_as_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows, st_ctime IS the creation time (there is no st_birthtime)."""
    monkeypatch.setattr(sys, "platform", "win32")
    fake = types.SimpleNamespace(st_mtime=1000.0, st_ctime=2000.0)
    monkeypatch.setattr(Path, "stat", lambda self, **kw: fake)
    assert read_file_times(tmp_path / "a").created == 2000


def test_birthtime_preferred_when_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When st_birthtime exists (macOS / statx Linux) it wins over ctime."""
    monkeypatch.setattr(sys, "platform", "win32")
    fake = types.SimpleNamespace(st_mtime=1000.0, st_ctime=2000.0, st_birthtime=7.0)
    monkeypatch.setattr(Path, "stat", lambda self, **kw: fake)
    assert read_file_times(tmp_path / "a").created == 7


def test_vanished_file_returns_zeros(tmp_path: Path) -> None:
    assert read_file_times(tmp_path / "nope.txt") == FileTimes(0, 0, 0)


def test_negative_timestamps_clamp_to_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Index fields are unsigned; a pre-epoch mtime must not go negative."""
    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    fake = types.SimpleNamespace(st_mtime=-5.0, st_ctime=-9.0, st_birthtime=-3.0)
    monkeypatch.setattr(Path, "stat", lambda self, **kw: fake)
    assert read_file_times(f) == FileTimes(0, 0, 0)

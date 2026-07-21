"""Filesystem timestamps for a file, read in one ``stat()``.

``created`` is ``st_birthtime``, which exists on macOS but not on Linux ext4
without ``statx``. Absent birthtime reports 0 rather than raising, so callers
get the same shape on every platform.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = ["FileTimes", "read_file_times"]


@dataclass(slots=True, frozen=True)
class FileTimes:
    """Unix seconds. 0 means unknown or unavailable, never "the epoch"."""

    mtime: int
    created: int
    inode_changed: int


def read_file_times(path: Path) -> FileTimes:
    """All three timestamps from one ``stat()``; zeros if the file vanished.

    Index fields are unsigned, so values clamp at 0.
    """
    try:
        st = path.stat()
    except OSError:
        return FileTimes(0, 0, 0)
    return FileTimes(
        mtime=max(int(st.st_mtime), 0),
        created=max(int(getattr(st, "st_birthtime", 0)), 0),
        inode_changed=max(int(st.st_ctime), 0),
    )

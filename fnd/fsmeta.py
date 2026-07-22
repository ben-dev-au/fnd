"""Filesystem timestamps for a file, read in one ``stat()``.

``created`` is best-effort per OS: ``st_birthtime`` on macOS (and statx-capable
Linux, e.g. ext4 on 3.12+); on Windows ``st_ctime`` *is* the creation time.
Where none is available it reports 0 rather than raising, so callers get the
same shape on every platform.
"""

from __future__ import annotations

import sys
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
    # created: st_birthtime on macOS + statx-capable Linux; on Windows
    # st_ctime is the creation time (not the POSIX inode-change time).
    birthtime = getattr(st, "st_birthtime", None)
    if birthtime is not None:
        created = max(int(birthtime), 0)
    elif sys.platform == "win32":
        created = max(int(st.st_ctime), 0)
    else:
        created = 0
    return FileTimes(
        mtime=max(int(st.st_mtime), 0),
        created=created,
        inode_changed=max(int(st.st_ctime), 0),
    )

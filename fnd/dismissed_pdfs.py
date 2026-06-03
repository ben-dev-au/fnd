"""User-dismissed flat PDFs.

Some PDFs are genuinely unstructured (scans, pure-image presentations)
and the user knows they will never texturise no matter how often the
pipeline retries. Recording their content hash here removes them from
the Flat PDFs list so the log stays a real to-do list of files
the user wants to fix, not a backlog of accepted-as-flat files.

Content-addressed (sha256 of the file bytes) so renaming or moving the
PDF preserves the dismissal; replacing it with new content invalidates
it. Mirrors the storage shape of fnd.seen_log: one zero-byte marker
file per sha under ``<user_data_dir>/dismissed/``."""

from __future__ import annotations

import contextlib
from pathlib import Path

from platformdirs import user_data_dir

_DISMISSED_DIRNAME = "dismissed"


def _dismissed_root() -> Path:
    # Read on every call so test fixtures that monkeypatch
    # ``user_data_dir`` see isolation immediately.
    return Path(user_data_dir("fnd")) / _DISMISSED_DIRNAME


def _marker_path(sha: str) -> Path:
    return _dismissed_root() / sha[:2] / sha


def is_dismissed(sha: str) -> bool:
    return _marker_path(sha).exists()


def mark_dismissed(sha: str) -> None:
    """Mark ``sha`` as user-accepted-as-flat. Idempotent."""
    path = _marker_path(sha)
    if path.exists():
        return
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


def undismiss(sha: str) -> None:
    """Remove a previous dismissal so the file rejoins the to-do log
    (used when the user wants to retry a previously-dismissed file)."""
    with contextlib.suppress(OSError):
        _marker_path(sha).unlink(missing_ok=True)


__all__ = ["is_dismissed", "mark_dismissed", "undismiss"]

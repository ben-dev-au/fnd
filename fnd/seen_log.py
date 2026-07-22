"""Per-content-hash 'has been indexed before' marker store.

Non-PDF extractors (md, txt, pptx, docx) don't use the structured-
extraction cache because they have nothing expensive to cache - their
extraction is already <1ms per file. Without a separate 'seen' record,
the runner has no way to distinguish 'this file is new to the index'
from 'this file was already indexed in a previous run', so non-PDFs
would always count as ``indexed_newly`` and an Update on a stable
corpus would mis-report '24 newly indexed' every launch.

Markers are tiny zero-byte files under ``<cache_root>/seen/<2-hex>/<sha>``.
The presence check is a single ``Path.exists()``; marking is a single
``Path.touch()``. Atomic on POSIX, content-addressed so file renames /
moves do not invalidate (extraction output depends on bytes, not path).
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from fnd import paths


def _seen_root() -> Path:
    # Kept as a function (not a cached constant) so test fixtures that
    # monkeypatch ``_seen_root`` see isolation immediately - a cached path
    # would bleed across tests and cause spurious "already indexed"
    # results on unrelated content.
    return paths.seen_dir()


def _marker_path(sha: str) -> Path:
    return _seen_root() / sha[:2] / sha


def has_seen(sha: str) -> bool:
    """True iff ``mark_seen(sha)`` was called in a prior run."""
    return _marker_path(sha).exists()


def mark_seen(sha: str) -> None:
    """Record that ``sha`` has been successfully indexed. Idempotent;
    a re-mark is cheap (single exists+touch)."""
    path = _marker_path(sha)
    if path.exists():
        return
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


def forget(sha: str) -> None:
    """Drop the seen-marker for ``sha`` so the next index pass reports the
    file as newly indexed. Used by a literal Rebuild, which genuinely
    re-does the work and must report it honestly. Idempotent."""
    with contextlib.suppress(OSError):
        _marker_path(sha).unlink()


__all__ = ["forget", "has_seen", "mark_seen"]

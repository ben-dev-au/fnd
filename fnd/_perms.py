"""Restrictive-permission helpers for files and directories under fnd's
application data tree.

By default ``Path.mkdir(parents=True)`` honours the process umask
(0o022 on macOS), which leaves directories world-readable. fnd's
``config.toml`` and on-disk state contain absolute paths to the user's
private documents — readable by any other local account on a shared
Mac. These helpers normalise everything we create to 0o700 dirs and
0o600 files so the published binary doesn't bake in the "single-user
Mac" assumption.

``secure_mkdir`` walks the chain from the app data root down to the
requested leaf, chmod-ing each segment we own. We deliberately do not
touch anything *above* the app data root (e.g. ``~/Library/Application
Support`` itself is OS-owned and should keep 0o755)."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path


def _chmod_quiet(p: Path, mode: int) -> None:
    """Best-effort chmod. Some filesystems (smbfs, exfat, some external
    drives) don't honour POSIX modes; swallowing the error is the right
    call — we tried."""
    with contextlib.suppress(OSError):
        os.chmod(p, mode)


def secure_mkdir(path: Path, *, anchor: Path | None = None) -> Path:
    """``mkdir -p`` + chmod 0o700 on every segment under ``anchor``
    (defaults to fnd's app data dir). Idempotent.

    Returns ``path`` so callers can chain.
    """
    if anchor is None:
        # Lazy import — ``fnd.config`` imports back from this module.
        from fnd.config import app_data_dir

        anchor = app_data_dir()
    anchor = anchor.expanduser()
    path = path.expanduser()
    path.mkdir(parents=True, exist_ok=True)
    try:
        rel = path.relative_to(anchor)
    except ValueError:
        # Caller asked for a dir outside our anchor — only chmod the leaf.
        _chmod_quiet(path, 0o700)
        return path
    cur = anchor
    _chmod_quiet(cur, 0o700)
    for part in rel.parts:
        cur = cur / part
        _chmod_quiet(cur, 0o700)
    return path


def secure_write_text(path: Path, text: str, *, atomic: bool = False) -> None:
    """Write ``text`` to ``path`` as UTF-8 with 0o600 perms.

    When ``atomic=True`` writes via a sibling ``.tmp`` + ``os.replace``
    so a crash mid-write can't leave the destination half-empty.
    """
    path = path.expanduser()
    if atomic:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        _chmod_quiet(tmp, 0o600)
        os.replace(tmp, path)
        _chmod_quiet(path, 0o600)
    else:
        path.write_text(text, encoding="utf-8")
        _chmod_quiet(path, 0o600)

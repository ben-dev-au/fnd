"""Filesystem locations — the single source of truth for every per-user
directory fnd reads or writes.

All app state lives under two roots resolved by ``platformdirs``:

* :func:`app_data_dir` — durable state (index, config, reindex-resume,
  keybindings, dismissed markers, calibration logs).
* :func:`app_cache_dir` — recomputable caches (PDF structure, seen-log,
  worker stderr).

Both pass ``appauthor=False`` so the two roots stay siblings on Windows
(``%LOCALAPPDATA%\\fnd\\…``). Passing it inconsistently splits app data
across ``…\\fnd\\`` and ``…\\fnd\\fnd\\``; on macOS/Linux ``appauthor`` is
ignored, so the split only appears on Windows. Every helper below is a pure
function — no directory is created here; callers create what they need
(often via :func:`fnd._perms.secure_mkdir`).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from platformdirs import user_cache_dir, user_data_dir

_APP_NAME = "fnd"


def app_data_dir() -> Path:
    """Root for durable per-user state."""
    return Path(user_data_dir(_APP_NAME, appauthor=False))


def app_cache_dir() -> Path:
    """Root for recomputable per-user caches."""
    return Path(user_cache_dir(_APP_NAME, appauthor=False))


# ── Data-root derivations ────────────────────────────────────────────────


def reindex_state_dir() -> Path:
    """Directory holding per-collection reindex-resume state files."""
    return app_data_dir() / "reindex"


def reindex_state_path(collection: str) -> Path:
    """Resume-state file for one ``collection``."""
    return reindex_state_dir() / f"{collection}.state.toml"


def dismissed_dir() -> Path:
    """Marker store for user-dismissed PDFs (sharded by sha prefix)."""
    return app_data_dir() / "dismissed"


def first_reindex_marker_path() -> Path:
    """Sentinel marking that the first-reindex cost warning was shown."""
    return app_data_dir() / "first_reindex_warning_seen"


def throughput_log_path() -> Path:
    """Indexer throughput calibration log (per user, not per venv)."""
    return app_data_dir() / "indexer_throughput.jsonl"


def failure_log_path() -> Path:
    """Per-(collection, file) extraction failure log."""
    return app_data_dir() / "indexer_failures.toml"


# ── Cache-root derivations ───────────────────────────────────────────────


def seen_dir() -> Path:
    """Marker store for the non-PDF "have we seen this content?" log."""
    return app_cache_dir() / "seen"


def worker_logs_dir() -> Path:
    """Directory for extractor-subprocess stderr redirection."""
    return app_cache_dir() / "worker-logs"


def pdf_structure_cache_dir() -> Path:
    """Content-addressed PDF structure extraction cache."""
    return app_cache_dir() / "pdf-structure"


# ── External tool locations ──────────────────────────────────────────────


def uv_tool_root() -> Path:
    """Root where ``uv tool install`` places tool venvs (the ``pdf-structure``
    extra installs docling here). Prefer uv's own answer (``uv tool dir``) so
    we track its layout on every OS; fall back to the platform default (POSIX
    XDG data dir / Windows ``%APPDATA%``) when uv isn't callable."""
    try:
        out = subprocess.run(
            ["uv", "tool", "dir"], capture_output=True, text=True, timeout=5, check=True
        ).stdout.strip()
        if out:
            return Path(out)
    except (OSError, subprocess.SubprocessError):
        pass
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "uv" / "tools"
    return Path.home() / ".local" / "share" / "uv" / "tools"

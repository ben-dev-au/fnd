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

import functools
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from platformdirs import user_cache_dir, user_data_dir

_APP_NAME = "fnd"


def app_data_dir() -> Path:
    """Root for durable per-user state."""
    return Path(user_data_dir(_APP_NAME, appauthor=False))


def app_cache_dir() -> Path:
    """Root for recomputable per-user caches."""
    return Path(user_cache_dir(_APP_NAME, appauthor=False))


def diagnostics_dir() -> Path:
    """Opt-in diagnostic logs: user-owned, not the shared temp dir, where a
    fixed file name is a link anyone can plant."""
    from fnd._perms import secure_mkdir

    return secure_mkdir(app_cache_dir() / "diagnostics")


# ── Data-root derivations ────────────────────────────────────────────────


def reindex_state_dir() -> Path:
    """Directory holding per-collection reindex-resume state files."""
    return app_data_dir() / "reindex"


def dismissed_dir() -> Path:
    """Marker store for user-dismissed PDFs (sharded by sha prefix)."""
    return app_data_dir() / "dismissed"


def first_reindex_marker_path() -> Path:
    """Sentinel marking that the first-reindex cost warning was shown."""
    return app_data_dir() / "first_reindex_warning_seen"


def throughput_log_path() -> Path:
    """Indexer throughput calibration log (per user, not per venv)."""
    return app_data_dir() / "indexer_throughput.jsonl"


def progress_calibration_path() -> Path:
    """Observed per-phase durations behind the progress line's pacing."""
    return app_data_dir() / "progress_calibration.jsonl"


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


@functools.cache
def _uv_tool_dir() -> str:
    """``uv tool dir``, asked once per process; ``""`` when uv isn't callable.

    Measured 60 ms warm / 90 ms cold, and a Settings row reaches it several
    times per screen open. uv's tool root does not move while fnd runs
    (installing a tool *into* it does not change it), so the answer is cached
    while the ``.exists()`` checks built on it stay live.
    """
    try:
        return subprocess.run(
            ["uv", "tool", "dir"], capture_output=True, text=True, timeout=5, check=True
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def uv_tool_root() -> Path:
    """Root where ``uv tool install`` places tool venvs (the ``pdf-structure``
    extra installs docling here). Prefer uv's own answer (``uv tool dir``) so
    we track its layout on every OS; fall back to the platform default (POSIX
    XDG data dir / Windows ``%APPDATA%``) when uv isn't callable."""
    out = _uv_tool_dir()
    if out:
        return Path(out)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "uv" / "tools"
    # POSIX: honour XDG_DATA_HOME (uv does) before the ~/.local/share default.
    xdg = os.environ.get("XDG_DATA_HOME")
    data_home = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return data_home / "uv" / "tools"


def storable(value: Any) -> Any:
    """``value`` with every string valid UTF-8: a file name that is not (held as
    lone surrogates) becomes visible escapes, so writing it to TOML cannot raise."""
    if isinstance(value, str):
        from fnd.display_text import escape_surrogates

        return escape_surrogates(value)
    if isinstance(value, dict):
        return {k: storable(v) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, list | tuple):
        return [storable(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
    return value


# Characters no Windows, macOS or Linux file name may hold, plus the reserved
# device names, which Windows refuses whatever follows the first dot.
_UNSAFE_NAME_CHARS = frozenset('<>:"/\\|?*') | frozenset(chr(c) for c in range(0x20))
_RESERVED_STEMS = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)


def safe_filename(name: str) -> str:
    """``name`` as a file name every filesystem takes: unchanged when it already
    is one, otherwise escaped and suffixed with a hash so two names never meet."""
    import hashlib

    plain = (
        name not in ("", ".", "..")
        and not set(name) & _UNSAFE_NAME_CHARS
        and not name.endswith((".", " "))
        and name.split(".", 1)[0].upper() not in _RESERVED_STEMS
    )
    if plain:
        return name
    escaped = "".join("_" if ch in _UNSAFE_NAME_CHARS or ch == "." else ch for ch in name)
    digest = hashlib.sha1(name.encode("utf-8", "surrogatepass"), usedforsecurity=False).hexdigest()
    return f"{escaped[:48]}-{digest[:10]}"


# What a terminal's drag-and-drop or tab completion escapes in a POSIX path.
_SHELL_ESCAPE = re.compile(r"\\([ \t'\"()\[\]{}&;!$`*?#~|<>])")


def _posix_pasted_path(text: str) -> str:
    return _SHELL_ESCAPE.sub(r"\1", text)


def _windows_pasted_path(text: str) -> str:
    """Unchanged: a backslash is a separator here, never an escape."""
    return text


# Keyed by sys.platform; anything not named is a POSIX shell.
_PASTED_PATH: dict[str, Callable[[str], str]] = {"win32": _windows_pasted_path}


def unescape_pasted_path(text: str) -> str:
    """A path as a shell on this platform would read it from a paste."""
    return _PASTED_PATH.get(sys.platform, _posix_pasted_path)(text)

"""The real ``fnd`` CLI in a child process, with its per-user data under a scratch dir.

platformdirs honours ``XDG_DATA_HOME`` on macOS and Linux but not on Windows, where
``WIN_PD_OVERRIDE_LOCAL_APPDATA`` is the override; without it the child reads and
writes the runner's real ``%LOCALAPPDATA%\\fnd``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def run_fnd(data_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """``fnd <args>`` in this venv, its config and index under ``data_root / "fnd"``."""
    env = {
        **os.environ,
        "XDG_DATA_HOME": str(data_root),
        "XDG_CACHE_HOME": str(data_root / "cache"),
        "WIN_PD_OVERRIDE_LOCAL_APPDATA": str(data_root),
        "PYTHONPATH": str(Path.cwd()),
        # A Windows pipe defaults to cp1252, which cannot encode the `→` `fnd index` prints.
        "PYTHONIOENCODING": "utf-8",
    }
    return subprocess.run(
        [sys.executable, "-m", "fnd", *args],
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )

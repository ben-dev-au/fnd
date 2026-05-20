"""MinerU runner. Opt-in via --with-mineru.

Spawns a long-running mineru daemon (one model load for the entire
bake-off run) via the mineru tool venv's Python. Each PDF is processed
via stdin/stdout JSON-RPC to the daemon — no subprocess cold-start per
PDF.
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path
from typing import Any

from tools.pdf_bakeoff._daemon import (
    DaemonState,
    extract_via_daemon,
    find_tool_python,
    install_hint_required,
    start_daemon,
    stop_daemon,
)
from tools.pdf_bakeoff.metrics import RunnerResult

NAME = "mineru"
_HELPER = Path(__file__).parent.parent / "helpers" / "mineru_helper.py"


def _check_macos_version() -> None:
    if sys.platform != "darwin":
        return
    try:
        major = int(platform.mac_ver()[0].split(".", 1)[0])
    except (ValueError, IndexError):
        return
    if major < 14:
        raise RuntimeError(f"MinerU requires macOS 14+ (Sonoma); detected {platform.mac_ver()[0]}")


def setup() -> Any:
    python = find_tool_python("mineru", "mineru")
    if python is None:
        raise ImportError(
            install_hint_required("mineru", "mineru", 'uv tool install "mineru[all]"')
        )
    _check_macos_version()
    print(f"[mineru] using {python}; loading model (~20s, one-time)…", file=sys.stderr)
    proc = start_daemon(python, _HELPER, name="mineru", ready_timeout_s=300.0)
    print("[mineru] daemon ready", file=sys.stderr)
    return DaemonState(proc=proc, docs={}, cli_name="mineru", helper=_HELPER)


def run(state: Any, pdf_path: Path, page_index: int) -> RunnerResult:
    _ = page_index
    return extract_via_daemon(state, pdf_path)


def teardown(state: Any) -> None:
    stop_daemon(state)

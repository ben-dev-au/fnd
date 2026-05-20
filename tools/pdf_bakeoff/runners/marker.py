"""Marker runner. Opt-in via --with-marker.

Spawns a long-running marker daemon (one model load for the entire
bake-off run) via the marker-pdf tool venv's Python. Each PDF is
processed via stdin/stdout JSON-RPC to the daemon — no subprocess
cold-start per PDF.
"""

from __future__ import annotations

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

NAME = "marker"
_HELPER = Path(__file__).parent.parent / "helpers" / "marker_helper.py"


def setup() -> Any:
    python = find_tool_python("marker-pdf", "marker_single")
    if python is None:
        raise ImportError(
            install_hint_required("marker", "marker_single", "uv tool install marker-pdf")
        )
    print(f"[marker] using {python}; loading model (~25s, one-time)…", file=sys.stderr)
    proc = start_daemon(python, _HELPER, name="marker", ready_timeout_s=300.0)
    print("[marker] daemon ready", file=sys.stderr)
    return DaemonState(proc=proc, docs={}, cli_name="marker_single", helper=_HELPER)


def run(state: Any, pdf_path: Path, page_index: int) -> RunnerResult:
    _ = page_index  # whole-doc runner; per-page granularity not modelled
    return extract_via_daemon(state, pdf_path)


def teardown(state: Any) -> None:
    stop_daemon(state)

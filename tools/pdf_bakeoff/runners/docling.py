"""Docling runner. Opt-in via --with-docling.

Spawns a long-running docling daemon (one model load for the entire
bake-off run) via the docling-slim tool venv's Python. Each PDF is
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

NAME = "docling"
_HELPER = Path(__file__).parent.parent / "helpers" / "docling_helper.py"


def setup() -> Any:
    python = find_tool_python("docling-slim", "docling")
    if python is None:
        raise ImportError(
            install_hint_required(
                "docling",
                "docling",
                'uv tool install "docling-slim[standard]"',
            )
        )
    print(f"[docling] using {python}; loading model (~15s, one-time)…", file=sys.stderr)
    proc = start_daemon(python, _HELPER, name="docling", ready_timeout_s=300.0)
    print("[docling] daemon ready", file=sys.stderr)
    return DaemonState(proc=proc, cli_name="docling", helper=_HELPER)


def run(state: Any, pdf_path: Path, page_index: int) -> RunnerResult:
    return extract_via_daemon(state, pdf_path, page_index)


def teardown(state: Any) -> None:
    stop_daemon(state)

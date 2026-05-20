"""Docling runner with aggressive M1-Max-tuned config.

Same daemon protocol as `docling`; just points at docling_tuned_helper.py
which configures the converter with num_threads=10, batch_size=16,
images_scale=0.5.
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

NAME = "docling_tuned"
_HELPER = Path(__file__).parent.parent / "helpers" / "docling_tuned_helper.py"


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
    print(f"[docling_tuned] using {python}; loading model (~15s, one-time)…", file=sys.stderr)
    proc = start_daemon(python, _HELPER, name="docling_tuned", ready_timeout_s=300.0)
    print("[docling_tuned] daemon ready", file=sys.stderr)
    return DaemonState(proc=proc, cli_name="docling", helper=_HELPER)


def run(state: Any, pdf_path: Path, page_index: int) -> RunnerResult:
    return extract_via_daemon(state, pdf_path, page_index)


def teardown(state: Any) -> None:
    stop_daemon(state)

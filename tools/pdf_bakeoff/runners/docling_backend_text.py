"""Docling runner with force_backend_text=True for inline-format recovery.

Sibling of `docling` — same model, but pulls text from the PDF backend
rather than reconstructing from the layout model. The PDF text layer
carries bold/italic glyph info that's lost in docling's default
reconstruction pipeline.
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

NAME = "docling_backend_text"
_HELPER = Path(__file__).parent.parent / "helpers" / "docling_backend_text_helper.py"


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
    print(
        f"[docling_backend_text] using {python}; loading model (~15s)…",
        file=sys.stderr,
    )
    proc = start_daemon(python, _HELPER, name="docling_backend_text", ready_timeout_s=300.0)
    print("[docling_backend_text] daemon ready", file=sys.stderr)
    return DaemonState(proc=proc, cli_name="docling", helper=_HELPER)


def run(state: Any, pdf_path: Path, page_index: int) -> RunnerResult:
    return extract_via_daemon(state, pdf_path, page_index)


def teardown(state: Any) -> None:
    stop_daemon(state)

"""pymupdf4llm with AI-based layout detection via `pymupdf.layout`.

The `pymupdf.layout` package adds ML-based layout analysis to
pymupdf4llm — improved table detection, list-item hierarchies, better
header/footer/paragraph classification. It's installed via the
`[layout]` extra and is Polyform Noncommercial-licensed (no commercial
use without paying Artifex).

Runs as a daemon subprocess so the `pymupdf.layout` import doesn't
side-effect the other pymupdf4llm runners in the harness.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from tools.pdf_bakeoff._daemon import (
    DaemonState,
    extract_via_daemon,
    start_daemon,
    stop_daemon,
)
from tools.pdf_bakeoff.metrics import RunnerResult

NAME = "pymupdf4llm_layout_ai"
_HELPER = Path(__file__).parent.parent / "helpers" / "pymupdf4llm_layout_ai_helper.py"


def _project_python() -> Path:
    """Return the project's own venv python (where pymupdf4llm[layout] is installed)."""
    return Path(sys.executable)


def setup() -> Any:
    python = _project_python()
    print(
        f"[pymupdf4llm_layout_ai] using {python}; spawning helper (one-time ~5s)…",
        file=sys.stderr,
    )
    proc = start_daemon(python, _HELPER, name="pymupdf4llm_layout_ai", ready_timeout_s=120.0)
    print("[pymupdf4llm_layout_ai] daemon ready", file=sys.stderr)
    return DaemonState(proc=proc, cli_name="python", helper=_HELPER)


def run(state: Any, pdf_path: Path, page_index: int) -> RunnerResult:
    return extract_via_daemon(state, pdf_path, page_index)


def teardown(state: Any) -> None:
    stop_daemon(state)

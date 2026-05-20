"""pymupdf4llm with default (layout-aware) extraction path."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pymupdf4llm  # type: ignore[import-untyped]

from tools.pdf_bakeoff.metrics import RunnerResult

NAME = "pymupdf4llm_layout"


def setup() -> Any:
    return None


def _extract(pdf_path: Path, page_index: int) -> str:
    chunks = pymupdf4llm.to_markdown(
        str(pdf_path),
        pages=[page_index],
        page_chunks=True,
        show_progress=False,
    )
    if not chunks:
        return ""
    first = chunks[0]
    if isinstance(first, dict):
        return str(first.get("text", ""))
    return str(first)


def run(_state: Any, pdf_path: Path, page_index: int) -> RunnerResult:
    t0 = time.perf_counter()
    try:
        md = _extract(pdf_path, page_index)
    except Exception as e:
        return RunnerResult(
            wall_ms=(time.perf_counter() - t0) * 1000.0,
            rss_delta_mb=0.0,
            output_md="",
            crashed=True,
            error=f"{type(e).__name__}: {e}",
        )
    return RunnerResult(
        wall_ms=(time.perf_counter() - t0) * 1000.0,
        rss_delta_mb=0.0,
        output_md=md,
    )

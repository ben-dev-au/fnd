"""Reference runner: page.get_text("text"). Sets the jaccard denominator."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pymupdf  # type: ignore[import-untyped]

from tools.pdf_bakeoff.metrics import RunnerResult

NAME = "baseline"


def setup() -> Any:
    return None


def run(_state: Any, pdf_path: Path, page_index: int) -> RunnerResult:
    t0 = time.perf_counter()
    try:
        doc = pymupdf.open(pdf_path)
        try:
            page = doc[page_index]
            text = page.get_text("text")
        finally:
            doc.close()
    except Exception as e:
        return RunnerResult(
            wall_ms=(time.perf_counter() - t0) * 1000.0,
            rss_delta_mb=0.0,
            output_md="",
            crashed=True,
            error=f"{type(e).__name__}: {e}",
        )
    wall_ms = (time.perf_counter() - t0) * 1000.0
    return RunnerResult(wall_ms=wall_ms, rss_delta_mb=0.0, output_md=text)

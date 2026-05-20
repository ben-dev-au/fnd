"""pymupdf4llm with default (layout-aware) extraction path."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pymupdf4llm  # type: ignore[import-untyped]

from tools.pdf_bakeoff._util import mute_fd
from tools.pdf_bakeoff.metrics import RunnerResult

NAME = "pymupdf4llm_layout"


def setup() -> Any:
    return None


def _extract(pdf_path: Path, page_index: int) -> str:
    # force_text=False alone disables the image-area OCR pass.
    # ignore_images=True keeps image-OCR off too. We DO process vector
    # graphics — most PDF tables are drawn as vector lines, and
    # ignore_graphics=True silently kills that detection path.
    # table_strategy="lines" is more permissive than "lines_strict".
    with mute_fd(1):
        chunks = pymupdf4llm.to_markdown(
            str(pdf_path),
            pages=[page_index],
            page_chunks=True,
            show_progress=False,
            force_text=False,
            ignore_images=True,
            ignore_graphics=False,
            table_strategy="lines",
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

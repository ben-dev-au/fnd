"""pymupdf4llm with TOC-based heading detection + permissive table strategy.

The default `to_markdown` path uses font-size heuristics and produces
mostly h2-only output. This variant supplies an explicit heading
detector:
- `TocHeaders` — uses the PDF's built-in table of contents when present,
  producing correctly-nested h1/h2/h3 hierarchy.
- `IdentifyHeaders(max_levels=6)` — explicit font-clustering fallback
  for PDFs with no TOC; tuned to look for more levels than the default.

Also uses `table_strategy="lines"` (vs the default `"lines_strict"`)
to detect more tables at the cost of some false positives.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pymupdf  # type: ignore[import-untyped]
import pymupdf4llm  # type: ignore[import-untyped]
from pymupdf4llm.helpers.pymupdf_rag import (  # type: ignore[import-untyped]
    IdentifyHeaders,
    TocHeaders,
)

from tools.pdf_bakeoff._util import mute_fd
from tools.pdf_bakeoff.metrics import RunnerResult

NAME = "pymupdf4llm_toc"


def setup() -> Any:
    return None


def run(_state: Any, pdf_path: Path, page_index: int) -> RunnerResult:
    t0 = time.perf_counter()
    try:
        doc = pymupdf.open(str(pdf_path))
        try:
            hdr_info = (
                TocHeaders(doc) if doc.get_toc() else IdentifyHeaders(str(pdf_path), max_levels=6)
            )
            with mute_fd(1):
                chunks = pymupdf4llm.to_markdown(
                    doc,
                    pages=[page_index],
                    page_chunks=True,
                    show_progress=False,
                    force_text=False,
                    ignore_images=True,
                    ignore_graphics=False,
                    hdr_info=hdr_info,
                    table_strategy="lines",
                )
        finally:
            doc.close()
        if not chunks:
            md = ""
        else:
            first = chunks[0]
            md = str(first.get("text", "")) if isinstance(first, dict) else str(first)
    except Exception as e:
        return RunnerResult(
            wall_ms=(time.perf_counter() - t0) * 1000.0,
            rss_delta_mb=0.0,
            output_md="",
            crashed=True,
            error=f"{type(e).__name__}: {e}",
            extra={"mode": "toc"},
        )
    return RunnerResult(
        wall_ms=(time.perf_counter() - t0) * 1000.0,
        rss_delta_mb=0.0,
        output_md=md,
        extra={"mode": "toc"},
    )

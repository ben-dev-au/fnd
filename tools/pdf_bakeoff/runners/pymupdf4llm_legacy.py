"""pymupdf4llm in glyph-based extraction mode (`use_glyphs=True`).

The default `to_markdown` path uses block-text extraction. `use_glyphs=True`
switches to glyph-by-glyph clustering, which can recover headings/font
hierarchy on PDFs where block extraction collapses sizing. Slower; the
bake-off measures whether it's worth the cost.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pymupdf4llm  # type: ignore[import-untyped]

from tools.pdf_bakeoff._util import mute_fd
from tools.pdf_bakeoff.metrics import RunnerResult

NAME = "pymupdf4llm_legacy"


def setup() -> Any:
    return None


def run(_state: Any, pdf_path: Path, page_index: int) -> RunnerResult:
    t0 = time.perf_counter()
    try:
        with mute_fd(1):
            chunks = pymupdf4llm.to_markdown(
                str(pdf_path),
                pages=[page_index],
                page_chunks=True,
                show_progress=False,
                use_glyphs=True,
                force_text=False,
                ignore_images=True,
                ignore_graphics=True,
            )
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
            extra={"mode": "use_glyphs"},
        )
    return RunnerResult(
        wall_ms=(time.perf_counter() - t0) * 1000.0,
        rss_delta_mb=0.0,
        output_md=md,
        extra={"mode": "use_glyphs"},
    )

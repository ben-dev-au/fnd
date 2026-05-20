"""pymupdf4llm legacy / font-clustering path.

The legacy path uses font heuristics instead of the layout pass added in
the 1.27 series. Different signatures across pymupdf4llm versions — we
probe `to_markdown` for known kwargs and fall back to layout-disabled
output via `dpi=0` if necessary. The runner pins which path it actually
used in `extra["mode"]` so RESULTS.md can distinguish runs.
"""

from __future__ import annotations

import inspect
import time
from pathlib import Path
from typing import Any

import pymupdf4llm  # type: ignore[import-untyped]

from tools.pdf_bakeoff.metrics import RunnerResult

NAME = "pymupdf4llm_legacy"


def _legacy_kwargs() -> tuple[dict[str, object], str]:
    """Pick a kwarg combo that disables the layout pass for this version."""
    sig = inspect.signature(pymupdf4llm.to_markdown)
    params = sig.parameters
    if "force_text" in params:
        return {"force_text": True}, "force_text"
    if "use_glyphs" in params:
        return {"use_glyphs": False}, "use_glyphs"
    if "layout" in params:
        return {"layout": False}, "layout"
    if "table_strategy" in params:
        return {"table_strategy": "lines_strict"}, "table_strategy_only"
    return {}, "no_legacy_flag_available"


def setup() -> Any:
    return _legacy_kwargs()


def run(state: Any, pdf_path: Path, page_index: int) -> RunnerResult:
    kwargs, mode = state if state else _legacy_kwargs()
    t0 = time.perf_counter()
    try:
        chunks = pymupdf4llm.to_markdown(
            str(pdf_path),
            pages=[page_index],
            page_chunks=True,
            show_progress=False,
            **kwargs,
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
            extra={"mode": mode},
        )
    return RunnerResult(
        wall_ms=(time.perf_counter() - t0) * 1000.0,
        rss_delta_mb=0.0,
        output_md=md,
        extra={"mode": mode},
    )

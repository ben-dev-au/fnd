"""Docling runner. Opt-in via --with-docling. Models cache locally."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

from platformdirs import user_cache_dir

from tools.pdf_bakeoff.metrics import RunnerResult

NAME = "docling"


def _cache_root() -> Path:
    return Path(user_cache_dir("fnd")) / "bakeoff" / "docling"


def setup() -> Any:
    try:
        from docling.document_converter import DocumentConverter  # type: ignore[import-not-found]
    except ImportError as e:
        raise ImportError("docling not installed. Install with: pip install docling") from e

    cache = _cache_root()
    cache.mkdir(parents=True, exist_ok=True)
    # Redirect Docling artifact downloads into our cache so first-run
    # weights live with the bake-off, not in HOME.
    os.environ.setdefault("DOCLING_ARTIFACTS_PATH", str(cache))

    print(
        f"[docling] artifacts dir: {cache}\n"
        "[docling] first run downloads ~200-400MB of model weights",
        file=sys.stderr,
    )
    return DocumentConverter()


def run(state: Any, pdf_path: Path, page_index: int) -> RunnerResult:
    converter = state
    t0 = time.perf_counter()
    try:
        # Docling's page selection is 1-based; align with our 0-based index.
        result = converter.convert(
            str(pdf_path),
            page_range=(page_index + 1, page_index + 1),
        )
        md = result.document.export_to_markdown()
    except TypeError:
        # Older docling signatures lack page_range; convert whole doc and
        # slice. Crude but keeps the runner usable across versions.
        result = converter.convert(str(pdf_path))
        md = result.document.export_to_markdown()
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

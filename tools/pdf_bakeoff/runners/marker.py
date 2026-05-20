"""Marker runner. Opt-in via --with-marker. GPL-3.0; see spec licensing matrix."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

from platformdirs import user_cache_dir

from tools.pdf_bakeoff.metrics import RunnerResult

NAME = "marker"


def _cache_root() -> Path:
    return Path(user_cache_dir("fnd")) / "bakeoff" / "marker"


def setup() -> Any:
    try:
        from marker.converters.pdf import PdfConverter  # type: ignore[import-not-found]
        from marker.models import create_model_dict  # type: ignore[import-not-found]
    except ImportError as e:
        raise ImportError("marker not installed. Install with: pip install marker-pdf") from e

    cache = _cache_root()
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(cache))
    # Apple Silicon: prefer MPS unless user pinned TORCH_DEVICE explicitly.
    os.environ.setdefault("TORCH_DEVICE", "mps" if sys.platform == "darwin" else "cpu")

    print(
        f"[marker] cache dir: {cache}\n"
        f"[marker] TORCH_DEVICE={os.environ.get('TORCH_DEVICE')}\n"
        "[marker] first run downloads ~5GB of model weights",
        file=sys.stderr,
    )
    return PdfConverter(artifact_dict=create_model_dict())


def _text_from(rendered: object) -> str:
    from marker.output import text_from_rendered  # type: ignore[import-not-found]

    text, _meta, _images = text_from_rendered(rendered)
    return text


def run(state: Any, pdf_path: Path, page_index: int) -> RunnerResult:
    converter = state
    t0 = time.perf_counter()
    try:
        rendered = converter(str(pdf_path), page_range=[page_index])
        md = _text_from(rendered)
    except TypeError:
        rendered = converter(str(pdf_path))
        md = _text_from(rendered)
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

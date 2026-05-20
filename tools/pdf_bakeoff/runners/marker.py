"""Marker runner. Opt-in via --with-marker. Invokes the `marker_single` CLI.

Installed via `uv tool install marker-pdf` (or `pipx install marker-pdf`,
or the user's system pip — anything that puts `marker_single` on PATH).
We use the CLI because marker-pdf pins pillow<11 which conflicts with
mineru's pillow>=11 — they can't share a venv.

marker_single supports --page_range, so we invoke it per page.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from platformdirs import user_cache_dir

from tools.pdf_bakeoff.metrics import RunnerResult

NAME = "marker"


def _cache_root() -> Path:
    return Path(user_cache_dir("fnd")) / "bakeoff" / "marker"


def setup() -> Any:
    if shutil.which("marker_single") is None:
        raise ImportError("marker_single CLI not on PATH. Install with: uv tool install marker-pdf")
    cache = _cache_root()
    cache.mkdir(parents=True, exist_ok=True)
    print(
        f"[marker] CLI: {shutil.which('marker_single')}\n[marker] cache dir: {cache}",
        file=sys.stderr,
    )
    return cache


def run(state: Any, pdf_path: Path, page_index: int) -> RunnerResult:
    _ = state
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="bakeoff-marker-") as tmp:
        out_dir = Path(tmp)
        cmd = [
            "marker_single",
            str(pdf_path),
            "--page_range",
            str(page_index),
            "--output_dir",
            str(out_dir),
            "--output_format",
            "markdown",
            "--disable_ocr",
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=600)
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            FileNotFoundError,
        ) as e:
            return RunnerResult(
                wall_ms=(time.perf_counter() - t0) * 1000.0,
                rss_delta_mb=0.0,
                output_md="",
                crashed=True,
                error=f"{type(e).__name__}: {e}",
            )
        md_files = list(out_dir.rglob("*.md"))
        md = md_files[0].read_text(encoding="utf-8", errors="replace") if md_files else ""
    return RunnerResult(
        wall_ms=(time.perf_counter() - t0) * 1000.0,
        rss_delta_mb=0.0,
        output_md=md,
    )

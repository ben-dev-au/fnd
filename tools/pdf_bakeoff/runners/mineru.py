"""MinerU runner. Opt-in via --with-mineru. Custom Apache-2.0-based license."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from platformdirs import user_cache_dir

from tools.pdf_bakeoff.metrics import RunnerResult

NAME = "mineru"


def _cache_root() -> Path:
    return Path(user_cache_dir("fnd")) / "bakeoff" / "mineru"


def _check_macos_version() -> None:
    if sys.platform != "darwin":
        return
    try:
        major = int(platform.mac_ver()[0].split(".", 1)[0])
    except (ValueError, IndexError):
        return
    if major < 14:
        raise RuntimeError(f"MinerU requires macOS 14+ (Sonoma); detected {platform.mac_ver()[0]}")


def setup() -> Any:
    try:
        import mineru  # type: ignore[import-not-found]  # noqa: F401
    except ImportError as e:
        raise ImportError(
            'mineru not installed. Install with: uv pip install -U "mineru[all]"'
        ) from e

    _check_macos_version()
    cache = _cache_root()
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MINERU_MODELS_DIR", str(cache))
    print(
        f"[mineru] models dir: {cache}\n" "[mineru] first run downloads model weights",
        file=sys.stderr,
    )
    return cache


def _read_first_md(out_dir: Path) -> str:
    """MinerU writes one .md per page into a structured output tree."""
    candidates = sorted(out_dir.rglob("*.md"))
    if not candidates:
        return ""
    return candidates[0].read_text(encoding="utf-8", errors="replace")


def run(state: Any, pdf_path: Path, page_index: int) -> RunnerResult:
    _ = state  # cache dir, unused at run time
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="bakeoff-mineru-") as tmp:
        out_dir = Path(tmp)
        # mineru's stable surface is the CLI; the Python API moves between
        # releases. Invoke the CLI for a single page.
        cmd = [
            "mineru",
            "-p",
            str(pdf_path),
            "-o",
            str(out_dir),
            "--start-page",
            str(page_index + 1),
            "--end-page",
            str(page_index + 1),
        ]
        try:
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=300,
            )
            md = _read_first_md(out_dir)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
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

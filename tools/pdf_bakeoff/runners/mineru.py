"""MinerU runner. Opt-in via --with-mineru. Invokes the `mineru` CLI.

Installed via `uv tool install "mineru[all]"` (or `pipx install
"mineru[all]"`) so it lands in an isolated env with the `mineru` binary
on PATH. mineru pulls heavy ML deps that conflict with marker-pdf's
pillow pin, so we don't put it in fnd's project venv.

Mineru's model load is ~20-30s. To avoid paying that for every page,
we invoke mineru ONCE per PDF (whole doc, txt method = no OCR), cache
the resulting markdown in-process, and serve the same markdown for
every page-call of that PDF.
"""

from __future__ import annotations

import os
import platform
import shutil
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
    if shutil.which("mineru") is None:
        raise ImportError('mineru CLI not on PATH. Install with: uv tool install "mineru[all]"')
    _check_macos_version()
    cache = _cache_root()
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MINERU_MODELS_DIR", str(cache))
    print(
        f"[mineru] CLI: {shutil.which('mineru')}\n[mineru] models dir: {cache}",
        file=sys.stderr,
    )
    return {"docs": {}}


def _read_first_md(out_dir: Path) -> str:
    candidates = sorted(out_dir.rglob("*.md"))
    if not candidates:
        return ""
    return candidates[0].read_text(encoding="utf-8", errors="replace")


def _extract_whole_doc(pdf_path: Path) -> tuple[str, float]:
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="bakeoff-mineru-") as tmp:
        out_dir = Path(tmp)
        cmd = [
            "mineru",
            "-p",
            str(pdf_path),
            "-o",
            str(out_dir),
            # txt-only mode: skip the OCR path for born-digital PDFs.
            "--method",
            "txt",
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=3600)
        md = _read_first_md(out_dir)
    return md, (time.perf_counter() - t0) * 1000.0


def run(state: Any, pdf_path: Path, page_index: int) -> RunnerResult:
    cache = state["docs"]
    key = str(pdf_path)
    if key in cache:
        return RunnerResult(wall_ms=0.0, rss_delta_mb=0.0, output_md=cache[key])
    _ = page_index
    try:
        md, wall_ms = _extract_whole_doc(pdf_path)
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ) as e:
        return RunnerResult(
            wall_ms=0.0,
            rss_delta_mb=0.0,
            output_md="",
            crashed=True,
            error=f"{type(e).__name__}: {e}",
        )
    cache[key] = md
    return RunnerResult(wall_ms=wall_ms, rss_delta_mb=0.0, output_md=md)

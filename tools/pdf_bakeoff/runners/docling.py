"""Docling runner. Opt-in via --with-docling. Invokes the `docling` CLI.

Installed via `uv tool install docling-slim` (or `pipx install
docling-slim`, or the user's system pip — anything that puts the
`docling` binary on PATH). Note: the `docling` package itself does not
ship a CLI entry point; `docling-slim` is the wrapper that does.
We use the CLI rather than a Python import because docling pins typer
<0.22 while fnd needs typer~=0.25 — they can't share a venv.

Docling does whole-doc extraction; per-page slicing isn't exposed on the
CLI. We cache the whole-doc markdown per (pdf_path) so subsequent
page-calls within the same PDF reuse it. Wall-time on the first call
is the real extraction cost; later calls are near-zero.
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

NAME = "docling"


def _cache_root() -> Path:
    return Path(user_cache_dir("fnd")) / "bakeoff" / "docling"


def setup() -> Any:
    if shutil.which("docling") is None:
        raise ImportError("docling CLI not on PATH. Install with: uv tool install docling-slim")
    cache = _cache_root()
    cache.mkdir(parents=True, exist_ok=True)
    print(
        f"[docling] CLI: {shutil.which('docling')}\n[docling] artifacts dir: {cache}",
        file=sys.stderr,
    )
    # State: per-pdf whole-doc cache + extraction wall-time per pdf.
    return {"docs": {}, "walls": {}, "cache_dir": cache}


def _extract_whole_doc(pdf_path: Path) -> tuple[str, float]:
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="bakeoff-docling-") as tmp:
        out_dir = Path(tmp)
        cmd = ["docling", str(pdf_path), "--to", "md", "--output", str(out_dir)]
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=600)
        md_files = list(out_dir.rglob("*.md"))
        md = md_files[0].read_text(encoding="utf-8", errors="replace") if md_files else ""
    return md, (time.perf_counter() - t0) * 1000.0


def run(state: Any, pdf_path: Path, page_index: int) -> RunnerResult:
    cache = state["docs"]
    walls = state["walls"]
    key = str(pdf_path)
    if key in cache:
        # Whole-doc already extracted; this page's marginal cost is ~0.
        return RunnerResult(wall_ms=0.0, rss_delta_mb=0.0, output_md=cache[key])

    _ = page_index  # whole-doc runner; page granularity lost
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
    walls[key] = wall_ms
    return RunnerResult(wall_ms=wall_ms, rss_delta_mb=0.0, output_md=md)

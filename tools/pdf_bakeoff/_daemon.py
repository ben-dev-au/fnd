"""Shared daemon-runner protocol for heavy ML extractors.

Spawns a tool's own Python interpreter running a helper script. The
helper loads its model once and streams JSON requests/responses over
stdin/stdout, so the bake-off pays one model-load cost per extractor
for the whole corpus instead of one per PDF.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, TypedDict

from tools.pdf_bakeoff.metrics import RunnerResult

# Each tool's CLI lives next to its own python interpreter when installed
# via `uv tool install <pkg>`. We resolve the python by walking from the
# `uv tool` link in ~/.local/bin/ to the real venv under
# ~/.local/share/uv/tools/<pkg>/bin/.
_UV_TOOL_ROOT = Path.home() / ".local" / "share" / "uv" / "tools"


class DaemonState(TypedDict):
    proc: subprocess.Popen[str]
    docs: dict[str, str]  # pdf_path -> extracted markdown
    cli_name: str
    helper: Path


def find_tool_python(uv_tool_pkg: str, cli_name: str) -> Path | None:
    """Find the Python interpreter inside a `uv tool install`'d package.

    Returns the path to the tool's bundled python, or None if the tool
    isn't installed (CLI not on PATH, or the venv layout is unexpected).
    """
    if shutil.which(cli_name) is None:
        return None
    candidate = _UV_TOOL_ROOT / uv_tool_pkg / "bin" / "python"
    if candidate.is_file() or candidate.is_symlink():
        return candidate
    return None


def start_daemon(
    python: Path, helper: Path, *, name: str, ready_timeout_s: float = 120.0
) -> subprocess.Popen[str]:
    """Spawn helper, block until it writes the `_status: ready` line."""
    # Pass helper stderr through to our stderr so device-detection /
    # config diagnostic lines are visible to the user.
    proc = subprocess.Popen(
        [str(python), str(helper)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        bufsize=1,
    )
    deadline = time.perf_counter() + ready_timeout_s
    assert proc.stdout is not None
    while time.perf_counter() < deadline:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError(f"{name} daemon exited before becoming ready")
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("_status") == "ready":
            return proc
    proc.kill()
    raise RuntimeError(f"{name} daemon did not become ready within {ready_timeout_s}s")


def extract_via_daemon(state: DaemonState, pdf_path: Path) -> RunnerResult:
    """Send one PDF to the daemon, read one JSON response."""
    cache = state["docs"]
    key = str(pdf_path)
    if key in cache:
        return RunnerResult(wall_ms=0.0, rss_delta_mb=0.0, output_md=cache[key])

    proc = state["proc"]
    assert proc.stdin is not None
    assert proc.stdout is not None

    try:
        proc.stdin.write(f"{pdf_path}\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
    except (BrokenPipeError, ValueError) as e:
        return RunnerResult(
            wall_ms=0.0,
            rss_delta_mb=0.0,
            output_md="",
            crashed=True,
            error=f"daemon-broken: {e}",
        )

    if not line:
        return RunnerResult(
            wall_ms=0.0,
            rss_delta_mb=0.0,
            output_md="",
            crashed=True,
            error="daemon-eof",
        )

    try:
        msg = json.loads(line)
    except json.JSONDecodeError as e:
        return RunnerResult(
            wall_ms=0.0,
            rss_delta_mb=0.0,
            output_md="",
            crashed=True,
            error=f"daemon-bad-json: {e}: {line[:200]!r}",
        )

    if "error" in msg:
        return RunnerResult(
            wall_ms=0.0,
            rss_delta_mb=0.0,
            output_md="",
            crashed=True,
            error=str(msg["error"]),
        )

    md = str(msg.get("md", ""))
    cache[key] = md
    return RunnerResult(
        wall_ms=float(msg.get("wall_ms", 0.0)),
        rss_delta_mb=0.0,
        output_md=md,
    )


def stop_daemon(state: DaemonState) -> None:
    proc = state["proc"]
    if proc.poll() is not None:
        return
    try:
        if proc.stdin is not None:
            proc.stdin.write("\n")
            proc.stdin.flush()
            proc.stdin.close()
        proc.wait(timeout=10)
    except (BrokenPipeError, subprocess.TimeoutExpired):
        proc.kill()


def install_hint_required(name: str, cli: str, install: str) -> str:
    return (
        f"{name} CLI ({cli!r}) not on PATH and/or tool venv not found at "
        f"{_UV_TOOL_ROOT}. Install with: {install}"
    )


_ = Any, sys  # quieten "unused" if a TypedDict alias changes

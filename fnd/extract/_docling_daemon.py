"""Long-running docling helper, spawned at most once per reindex run.

Docling's Python API can't share fnd's project venv (typer<0.22 vs
fnd's typer~=0.25). The workaround proven in the Phase 0 bake-off:
spawn docling's *own* venv Python running a small helper script, talk
to it over stdin/stdout JSON-RPC.

Used by the production `fnd/extract/pdf.py` Phase 3 routing to extract
pages where pymupdf4llm emitted `==> picture intentionally omitted <==`
markers (image-rendered tables).

Lifecycle:
- `DoclingDaemon.get()` returns a process-singleton instance, spawning
  on first call (~3s model load) and reusing for every subsequent
  page in the reindex run.
- `DoclingDaemon.shutdown()` is called from the indexer at end-of-run
  (or atexit as a safety net).
- If `uv tool install docling-slim[standard]` was never run, .get()
  returns None and callers fall through to whatever they had.
"""

from __future__ import annotations

import atexit
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

_UV_TOOL_ROOT = Path.home() / ".local" / "share" / "uv" / "tools"
_HELPER_SCRIPT = Path(__file__).parent / "_docling_helper.py"


class DoclingDaemon:
    """Singleton wrapper around a docling helper subprocess."""

    _instance: DoclingDaemon | None = None

    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self._proc = proc

    @classmethod
    def get(cls) -> DoclingDaemon | None:
        """Return the singleton, spawning it on first call. Returns
        None when docling isn't installed (caller falls through)."""
        if cls._instance is not None:
            return cls._instance
        python = _docling_python()
        if python is None:
            return None
        proc = _spawn_helper(python)
        if proc is None:
            return None
        cls._instance = cls(proc)
        atexit.register(cls.shutdown)
        return cls._instance

    @classmethod
    def shutdown(cls) -> None:
        if cls._instance is None:
            return
        proc = cls._instance._proc
        cls._instance = None
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

    def extract_page(self, pdf_path: Path, page_index: int, *, timeout_s: float = 45.0) -> str:
        """Send one (pdf, page) request; return the Markdown response.

        Returns "" on any failure — caller decides how to handle
        (typically: keep the pymupdf4llm output it already has).

        The blocking stdout.readline() is wrapped in a select() with a
        timeout so a docling wedge (image-rich tables hang its
        TableFormer model) doesn't take down the whole worker via the
        120s stall detector. On timeout we kill the daemon so the next
        call gets a fresh one - docling state can be poisoned after a
        wedge and a soft re-request would just hang again."""
        import select

        assert self._proc.stdin is not None
        assert self._proc.stdout is not None
        request = json.dumps({"pdf": str(pdf_path), "page": page_index})
        try:
            self._proc.stdin.write(request + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            return ""
        ready, _, _ = select.select([self._proc.stdout], [], [], timeout_s)
        if not ready:
            # Docling didn't respond in time. Tear it down so the next
            # page doesn't inherit the wedged state.
            type(self).shutdown()
            return ""
        try:
            line = self._proc.stdout.readline()
        except (BrokenPipeError, ValueError):
            return ""
        if not line:
            return ""
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return ""
        if "error" in msg:
            return ""
        return str(msg.get("md", ""))


def _docling_python() -> Path | None:
    if shutil.which("docling") is None:
        return None
    candidate = _UV_TOOL_ROOT / "docling-slim" / "bin" / "python"
    if candidate.is_file() or candidate.is_symlink():
        return candidate
    return None


def _spawn_helper(python: Path, ready_timeout_s: float = 60.0) -> subprocess.Popen[str] | None:
    # -I = isolated mode: don't add the script's dir to sys.path[0] and
    # ignore PYTHONPATH. The helper lives in fnd/extract/, which contains
    # fnd's own pptx.py extractor — without isolated mode, docling's
    # `import pptx` resolves to that file instead of the docling-slim
    # venv's python-pptx package, breaking the helper at import time.
    proc = subprocess.Popen(
        [str(python), "-I", str(_HELPER_SCRIPT)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        text=True,
        bufsize=1,
    )
    deadline = time.perf_counter() + ready_timeout_s
    assert proc.stdout is not None
    while time.perf_counter() < deadline:
        line = proc.stdout.readline()
        if not line:
            return None
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("_status") == "ready":
            return proc
    proc.kill()
    return None

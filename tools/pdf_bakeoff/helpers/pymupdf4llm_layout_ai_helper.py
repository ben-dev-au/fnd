"""Daemon helper for pymupdf4llm with AI-based layout detection.

`pymupdf.layout` must be imported BEFORE `pymupdf4llm` to enable AI-mode.
The import has process-global side effects (changes flag validation,
ML model registered as the layout engine), so we isolate this variant
in its own subprocess to avoid contaminating the other pymupdf4llm
runners in the harness.

License note: pymupdf-layout is Polyform Noncommercial 1.0 / commercial
(Artifex). Acceptable here because fnd is open-source and non-commercial.

Protocol:
  request:  {"pdf": "...", "page": 0}
  response: {"pdf": "...", "page": 0, "wall_ms": 1234.5, "md": "..."}
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time


def _main() -> None:
    log_fd = os.dup(1)
    null_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null_fd, 1)
        import pymupdf.layout  # noqa: F401  - side-effect import; must precede pymupdf4llm
        import pymupdf4llm  # type: ignore[import-untyped]

        pymupdf4llm.use_layout(True)
    finally:
        os.dup2(log_fd, 1)
        os.close(log_fd)
        os.close(null_fd)

    print("[pymupdf4llm-layout-ai-helper] use_layout(True)", file=sys.stderr, flush=True)

    sys.stdout.write(json.dumps({"_status": "ready"}) + "\n")
    sys.stdout.flush()

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            break
        req = json.loads(raw)
        pdf = req["pdf"]
        page = int(req["page"])
        t0 = time.perf_counter()
        try:
            with _mute_fd(1), _mute_fd(2):
                # Layout mode rejects `ignore_images=True + force_text=False`
                # (its validator considers that double-suppression). Let
                # layout mode use its own defaults; the layout pipeline
                # handles images without OCR by classifying them at the
                # layout level.
                chunks = pymupdf4llm.to_markdown(
                    pdf,
                    pages=[page],
                    page_chunks=True,
                    show_progress=False,
                    force_text=False,
                )
            if not chunks:
                md = ""
            else:
                first = chunks[0]
                md = str(first.get("text", "")) if isinstance(first, dict) else str(first)
            wall_ms = (time.perf_counter() - t0) * 1000.0
            payload = {"pdf": pdf, "page": page, "wall_ms": wall_ms, "md": md}
        except Exception as e:
            payload = {"pdf": pdf, "page": page, "error": f"{type(e).__name__}: {e}"}
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()


@contextlib.contextmanager
def _mute_fd(fd):
    saved = os.dup(fd)
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null, fd)
        yield
    finally:
        os.dup2(saved, fd)
        os.close(saved)
        os.close(null)


if __name__ == "__main__":
    _main()

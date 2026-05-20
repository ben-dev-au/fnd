"""Daemon helper for docling. Loads model once; extracts one page per request.

Executed by docling-slim's own tool-venv Python, not fnd's venv.

Protocol:
  request:  {"pdf": "...", "page": 0}   (page is 0-based)
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
        from docling.datamodel.base_models import InputFormat  # type: ignore[import-not-found]
        from docling.datamodel.pipeline_options import (
            PdfPipelineOptions,  # type: ignore[import-not-found]
        )
        from docling.document_converter import (  # type: ignore[import-not-found]
            DocumentConverter,
            PdfFormatOption,
        )

        pipe_opts = PdfPipelineOptions()
        pipe_opts.do_ocr = False
        converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipe_opts)}
        )

        try:
            import torch  # type: ignore[import-not-found]

            device_actual = (
                "mps"
                if torch.backends.mps.is_available()
                else ("cuda" if torch.cuda.is_available() else "cpu")
            )
        except Exception:
            device_actual = "unknown"
    finally:
        os.dup2(log_fd, 1)
        os.close(log_fd)
        os.close(null_fd)

    print(f"[docling-helper] device={device_actual} do_ocr=False", file=sys.stderr, flush=True)

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
                # docling page_range is 1-based, inclusive on both ends.
                result = converter.convert(pdf, page_range=(page + 1, page + 1))
                md = result.document.export_to_markdown()
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

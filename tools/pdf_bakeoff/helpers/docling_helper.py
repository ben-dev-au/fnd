"""Daemon helper for docling. Loads model once, processes PDFs from stdin.

Executed by docling-slim's own tool-venv Python, not fnd's venv.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time


def _main() -> None:
    # Quiet model-load progress to avoid corrupting the stdout JSON stream.
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
    finally:
        os.dup2(log_fd, 1)
        os.close(log_fd)
        os.close(null_fd)

    sys.stdout.write(json.dumps({"_status": "ready"}) + "\n")
    sys.stdout.flush()

    for raw in sys.stdin:
        pdf = raw.strip()
        if not pdf:
            break
        t0 = time.perf_counter()
        try:
            with _mute_fd(1), _mute_fd(2):
                result = converter.convert(pdf)
                md = result.document.export_to_markdown()
            wall_ms = (time.perf_counter() - t0) * 1000.0
            payload = {"pdf": pdf, "wall_ms": wall_ms, "md": md}
        except Exception as e:
            payload = {"pdf": pdf, "error": f"{type(e).__name__}: {e}"}
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

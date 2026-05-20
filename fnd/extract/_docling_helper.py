"""Docling helper subprocess — runs under docling-slim's own tool venv.

Reads PDF page requests from stdin as JSON lines; writes one JSON
response per request to stdout. Loads the docling model once at
startup. Used by `fnd.extract._docling_daemon.DoclingDaemon`.

NOT executed in fnd's project venv. fnd never imports this directly —
it's launched as a subprocess via `python <this_file>`.

Protocol:
  startup:  {"_status": "ready"}
  request:  {"pdf": "...", "page": 0}            (page is 0-based)
  response: {"pdf": "...", "page": 0, "md": "..."} OR {"error": "..."}
  shutdown: empty line on stdin → exit
"""

from __future__ import annotations

import contextlib
import json
import os
import sys


def _main() -> None:
    log_fd = os.dup(1)
    null_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null_fd, 1)
        from docling.datamodel.base_models import InputFormat  # type: ignore[import-not-found]
        from docling.datamodel.pipeline_options import (  # type: ignore[import-not-found]
            PdfPipelineOptions,
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
        raw = raw.strip()
        if not raw:
            break
        try:
            req = json.loads(raw)
            pdf = req["pdf"]
            page = int(req["page"])
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            sys.stdout.write(json.dumps({"error": f"bad-request: {e}"}) + "\n")
            sys.stdout.flush()
            continue
        try:
            with _mute_fd(1), _mute_fd(2):
                result = converter.convert(pdf, page_range=(page + 1, page + 1))
                md = result.document.export_to_markdown()
            payload = {"pdf": pdf, "page": page, "md": md}
        except Exception as e:
            payload = {"pdf": pdf, "page": page, "error": f"{type(e).__name__}: {e}"}
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()


@contextlib.contextmanager
def _mute_fd(fd: int):
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

"""Daemon helper for marker. Loads model once; extracts one page per request.

Executed by marker-pdf's own tool-venv Python, not fnd's venv.

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
    os.environ.setdefault("TORCH_DEVICE", "mps" if sys.platform == "darwin" else "cpu")

    log_fd = os.dup(1)
    null_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null_fd, 1)
        from marker.config.parser import ConfigParser  # type: ignore[import-not-found]
        from marker.converters.pdf import PdfConverter  # type: ignore[import-not-found]
        from marker.models import create_model_dict  # type: ignore[import-not-found]
        from marker.output import text_from_rendered  # type: ignore[import-not-found]
        from marker.settings import settings  # type: ignore[import-not-found]

        device_actual = settings.TORCH_DEVICE_MODEL
        artifact_dict = create_model_dict()
    finally:
        os.dup2(log_fd, 1)
        os.close(log_fd)
        os.close(null_fd)

    print(f"[marker-helper] device={device_actual}", file=sys.stderr, flush=True)

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
                config = ConfigParser(
                    {
                        "disable_ocr": True,
                        "disable_image_extraction": True,
                        "disable_ocr_math": True,
                        "disable_links": True,
                        "page_range": str(page),
                    }
                ).generate_config_dict()
                converter = PdfConverter(artifact_dict=artifact_dict, config=config)
                rendered = converter(pdf)
                md, _meta, _images = text_from_rendered(rendered)
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

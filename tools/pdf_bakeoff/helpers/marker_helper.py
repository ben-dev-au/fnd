"""Daemon helper for marker. Loads model once, processes PDFs from stdin.

Executed by marker-pdf's own tool-venv Python, not fnd's venv.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time


def _main() -> None:
    os.environ.setdefault("TORCH_DEVICE", "mps" if sys.platform == "darwin" else "cpu")

    # Aggressive batch sizes — marker's defaults are tuned for ~8GB
    # devices. On 32-64GB Apple Silicon there's no reason to single-
    # thread the model. Numbers below are conservative for a 64GB Max;
    # halve them if you OOM.
    marker_config = {
        "disable_ocr": True,
        "disable_image_extraction": True,
        "disable_ocr_math": True,
        "disable_links": True,
        "layout_batch_size": 32,
        "detection_batch_size": 32,
        "equation_batch_size": 32,
        "table_rec_batch_size": 16,
        "pdftext_workers": 8,
    }

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
        config = ConfigParser(marker_config).generate_config_dict()
        converter = PdfConverter(artifact_dict=create_model_dict(), config=config)
    finally:
        os.dup2(log_fd, 1)
        os.close(log_fd)
        os.close(null_fd)

    print(
        f"[marker-helper] device={device_actual} "
        f"layout_batch={marker_config['layout_batch_size']} "
        f"det_batch={marker_config['detection_batch_size']} "
        f"pdftext_workers={marker_config['pdftext_workers']}",
        file=sys.stderr,
        flush=True,
    )

    sys.stdout.write(json.dumps({"_status": "ready"}) + "\n")
    sys.stdout.flush()

    for raw in sys.stdin:
        pdf = raw.strip()
        if not pdf:
            break
        t0 = time.perf_counter()
        try:
            with _mute_fd(1), _mute_fd(2):
                rendered = converter(pdf)
                md, _meta, _images = text_from_rendered(rendered)
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

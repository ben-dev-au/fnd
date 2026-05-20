"""Daemon helper for mineru. Loads model once; extracts one page per request.

Executed by mineru's own tool-venv Python, not fnd's venv.

Protocol:
  request:  {"pdf": "...", "page": 0}   (page is 0-based)
  response: {"pdf": "...", "page": 0, "wall_ms": 1234.5, "md": "..."}
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path


def _main() -> None:
    log_fd = os.dup(1)
    null_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null_fd, 1)
        from mineru.cli.common import do_parse  # type: ignore[import-not-found]

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

    print(
        f"[mineru-helper] device={device_actual} backend=pipeline method=txt",
        file=sys.stderr,
        flush=True,
    )

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
                md = _extract_one_page(do_parse, Path(pdf), page)
            wall_ms = (time.perf_counter() - t0) * 1000.0
            payload = {"pdf": pdf, "page": page, "wall_ms": wall_ms, "md": md}
        except Exception as e:
            payload = {"pdf": pdf, "page": page, "error": f"{type(e).__name__}: {e}"}
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()


def _extract_one_page(do_parse, pdf_path: Path, page_index: int) -> str:
    pdf_bytes = pdf_path.read_bytes()
    with tempfile.TemporaryDirectory(prefix="mineru-helper-") as tmp:
        do_parse(
            output_dir=tmp,
            pdf_file_names=[pdf_path.stem],
            pdf_bytes_list=[pdf_bytes],
            p_lang_list=["en"],
            backend="pipeline",
            parse_method="txt",
            formula_enable=False,
            table_enable=True,
            f_draw_layout_bbox=False,
            f_draw_span_bbox=False,
            f_dump_md=True,
            f_dump_middle_json=False,
            f_dump_model_output=False,
            f_dump_orig_pdf=False,
            f_dump_content_list=False,
            image_analysis=False,
            start_page_id=page_index,
            end_page_id=page_index,
        )
        md_files = list(Path(tmp).rglob("*.md"))
        if not md_files:
            return ""
        return md_files[0].read_text(encoding="utf-8", errors="replace")


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

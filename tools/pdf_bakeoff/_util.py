"""Shared helpers for the bake-off harness."""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator


@contextlib.contextmanager
def mute_fd(fd: int) -> Iterator[None]:
    """Temporarily redirect a file descriptor to /dev/null.

    Used to silence libmupdf's stdout banner ("=== Document parser
    messages ===" + Tesseract init line) during pymupdf4llm calls.
    The banner prints from C-level code and isn't catchable by
    `contextlib.redirect_stdout`.
    """
    saved = os.dup(fd)
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null, fd)
        yield
    finally:
        os.dup2(saved, fd)
        os.close(saved)
        os.close(null)

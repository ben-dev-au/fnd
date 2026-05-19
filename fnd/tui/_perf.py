"""Env-gated perf spans for preview-pipeline measurement.

Toggle with ``_FND_PERF=1``. When disabled, the context manager and
``mark()`` calls are near-zero cost (one env lookup at import time).
Records live in a process-global ring; tests reset between scenarios.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Generator
from contextlib import contextmanager
from threading import Lock
from typing import Any

_ENABLED: bool = os.environ.get("_FND_PERF") == "1"
_RECORD: list[dict[str, Any]] = []
_LOCK = Lock()
_T0 = time.perf_counter()


def enabled() -> bool:
    return _ENABLED


def reset() -> None:
    global _T0
    with _LOCK:
        _RECORD.clear()
        _T0 = time.perf_counter()  # pyright: ignore[reportConstantRedefinition]


@contextmanager
def span(name: str, **meta: Any) -> Generator[None]:
    if not _ENABLED:
        yield
        return
    start = time.perf_counter()
    yield
    end = time.perf_counter()
    with _LOCK:
        _RECORD.append(
            {
                "name": name,
                "kind": "span",
                "ms": (end - start) * 1000.0,
                "t_start_ms": (start - _T0) * 1000.0,
                **meta,
            }
        )


def mark(name: str, **meta: Any) -> None:
    if not _ENABLED:
        return
    now = time.perf_counter()
    with _LOCK:
        _RECORD.append(
            {
                "name": name,
                "kind": "mark",
                "t_ms": (now - _T0) * 1000.0,
                **meta,
            }
        )


def records() -> list[dict[str, Any]]:
    with _LOCK:
        return list(_RECORD)


def dump_json(indent: int = 2) -> str:
    return json.dumps(records(), indent=indent)

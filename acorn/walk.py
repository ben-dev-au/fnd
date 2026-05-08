"""Filesystem walker — yield supported files under given roots.

Phase 1: simple recursive walk filtered by extension. Includes/excludes globs
and mtime gating land in phase 3 alongside collections.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

from acorn.extract import supported_suffixes


def walk(roots: Iterable[Path]) -> Iterator[Path]:
    suffixes = supported_suffixes()
    for root in roots:
        root = root.expanduser().resolve()
        if root.is_file():
            if root.suffix.lower() in suffixes:
                yield root
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() not in suffixes:
                continue
            # Skip hidden files and __pycache__.
            if any(part.startswith(".") for part in p.relative_to(root).parts):
                continue
            if "__pycache__" in p.parts:
                continue
            yield p

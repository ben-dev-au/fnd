"""Filesystem walker — yield supported files under a collection's roots.

Includes/excludes precedence per plan §8:

1. A path is in scope only if it lives under one of ``roots``.
2. If ``includes`` is set, the path must match at least one ``includes`` glob.
3. If the path matches **any** ``excludes`` glob, it is dropped — even if it
   matched an ``includes``.
4. Hidden files (``.foo``) are excluded by default unless an explicit include
   matches.
5. Symlinks are followed only if ``follow_symlinks = True``.

Globs are matched against the path **relative to its root** using ``PurePath.match``-
compatible semantics extended to support ``**`` (recursive).
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Iterator
from pathlib import Path

from acorn.extract import supported_suffixes


def _matches_any(globs: list[str], rel_str: str) -> bool:
    """Return True if ``rel_str`` matches any glob.

    ``**`` matches any number of path segments; ``fnmatch`` already handles
    that correctly when ``/`` is a regular character in the pattern.
    """
    return any(fnmatch.fnmatchcase(rel_str, g) for g in globs)


def _is_hidden(rel: Path) -> bool:
    return any(part.startswith(".") for part in rel.parts)


def _glob_targets_hidden(globs: list[str]) -> bool:
    """True if any include pattern explicitly references a dot-prefixed
    component (e.g. ``.git/**`` or ``**/.foo/**``)."""
    for g in globs:
        for part in g.split("/"):
            if part.startswith("."):
                return True
    return False


def walk(
    *,
    roots: Iterable[Path],
    includes: list[str] | None = None,
    excludes: list[str] | None = None,
    follow_symlinks: bool = False,
) -> Iterator[Path]:
    suffixes = supported_suffixes()
    inc = list(includes or [])
    exc = list(excludes or [])

    for root in roots:
        root = root.expanduser().resolve()
        if not root.exists():
            continue
        if root.is_file():
            if root.suffix.lower() in suffixes:
                yield root
            continue

        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if not follow_symlinks and p.is_symlink():
                continue
            if p.suffix.lower() not in suffixes:
                continue

            try:
                rel = p.relative_to(root)
            except ValueError:
                continue
            rel_str = str(rel)

            # Hidden by default; only included when a pattern explicitly
            # references a dot-prefixed component (so a blanket "**/*.md"
            # does NOT pull in .git/notes.md).
            if _is_hidden(rel) and not _glob_targets_hidden(inc):
                continue

            # Include whitelist (optional).
            if inc and not _matches_any(inc, rel_str):
                continue

            # Exclude blacklist always wins.
            if exc and _matches_any(exc, rel_str):
                continue

            yield p

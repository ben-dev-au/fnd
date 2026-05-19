"""Filesystem walker — yield supported files under a collection's roots.

Includes/excludes precedence per plan §8:

1. A path is in scope only if it lives under one of ``roots``.
2. If ``includes`` is set, the path must match at least one ``includes`` glob.
3. If the path matches **any** ``excludes`` glob, it is dropped — even if it
   matched an ``includes``.
4. Hidden files (``.foo``) are excluded by default unless an explicit include
   matches.
5. Symlinks are followed only if ``follow_symlinks = True``. This applies in
   two places:
   - The collection root itself — if the user-supplied ``root`` is a symlink,
     it is refused unless ``follow_symlinks=True``. This blocks a hostile
     config (or a typo) from pointing fnd at ``/etc`` via a symlinked root.
   - Each file inside the tree — symlinked files are skipped when the flag is
     off. Directory symlinks are not recursed into (we pass
     ``recurse_symlinks=False`` to ``Path.rglob`` rather than relying on the
     Python 3.13 default).

Globs are matched against the path **relative to its root** using ``PurePath.match``-
compatible semantics extended to support ``**`` (recursive).
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fnd.config import SourceConfig

from fnd.extract import supported_suffixes


def _matches_any(globs: list[str], rel_str: str) -> bool:
    """Return True if ``rel_str`` matches any glob.

    ``**`` matches any number of path segments, including zero — so
    ``**/*.md`` must match both ``sub/a.md`` *and* ``a.md`` (root-level).
    ``fnmatch`` treats ``**`` as a literal wildcard across ``/`` characters
    which covers the subdir case but not the zero-segment case, so for
    patterns that start with ``**/`` we also try the pattern without that
    prefix against root-level paths (no ``/`` in ``rel_str``).
    """
    root_level = "/" not in rel_str
    for g in globs:
        if fnmatch.fnmatchcase(rel_str, g):
            return True
        # ``**/*.ext`` should match ``a.ext`` at the root level too.
        if root_level and g.startswith("**/") and fnmatch.fnmatchcase(rel_str, g[3:]):
            return True
    return False


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
        original = root.expanduser()
        if not follow_symlinks and original.is_symlink():
            # A symlinked root is the only way the index can end up
            # following the link target (the inner symlink-checks below
            # only handle members). Refuse unless the user opted in.
            continue
        root = original.resolve()
        if not root.exists():
            continue
        if root.is_file():
            if root.suffix.lower() in suffixes:
                yield root
            continue

        for p in root.rglob("*", recurse_symlinks=False):
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


def walk_sources(*, sources: list[SourceConfig]) -> Iterator[Path]:
    """Yield in-scope paths across every source.

    Per source: applies includes/excludes via :func:`walk`, then on
    ``.md`` files runs the source's frontmatter filter. Frontmatter parse
    errors and missing-field strict-null cases drop the file silently —
    the indexer will eventually log them via ``fnd status --errors``
    (phase 10).
    """
    from fnd.config import SourceConfig  # local import: avoid cycle
    from fnd.filter_dsl import compile_filter
    from fnd.frontmatter import (
        FrontmatterParseError,
        read_frontmatter_from_file,
    )

    for source in sources:
        assert isinstance(source, SourceConfig)
        predicate = compile_filter(source.frontmatter_filter) if source.frontmatter_filter else None
        for path in walk(
            roots=[source.path],
            includes=source.includes or None,
            excludes=source.excludes or None,
            follow_symlinks=source.follow_symlinks,
        ):
            if predicate is None or path.suffix.lower() != ".md":
                yield path
                continue
            try:
                fm = read_frontmatter_from_file(path) or {}
            except FrontmatterParseError:
                continue
            if predicate(fm):
                yield path

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
import os
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fnd.config import SourceConfig

from fnd.extract import supported_suffixes

# macOS "Optimize Mac Storage" marker for an iCloud-offloaded placeholder.
# stat(2)'s st_flags carries this bit when the file's contents have been
# evicted from local disk; reading the file would synchronously download it.
_SF_DATALESS = 0x40000000


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


def resolve_skip_dirs(defaults: object | None = None) -> frozenset[str]:
    """Return the directory-basename prune set for the active defaults.

    Indexer entry points pass the full ``Config.defaults`` so user
    overrides (disable, extend) take effect. ``None`` resolves to the
    built-in :data:`fnd.config.DEFAULT_JUNK_DIRS`. An empty frozenset
    disables the prune (the rest of the walk still applies the existing
    hidden-file and per-source ``excludes`` rules).
    """
    from fnd.config import DEFAULT_JUNK_DIRS

    if defaults is None:
        return DEFAULT_JUNK_DIRS
    skip = bool(getattr(defaults, "skip_junk_dirs", True))
    if not skip:
        return frozenset()
    extras = tuple(getattr(defaults, "extra_junk_dirs", ()) or ())
    if not extras:
        return DEFAULT_JUNK_DIRS
    return DEFAULT_JUNK_DIRS | frozenset(extras)


def is_dataless(path: Path) -> bool:
    """True if ``path`` is an iCloud-offloaded placeholder on macOS.

    Reading the file would trigger a synchronous download. Detected via
    the SF_DATALESS st_flag bit. Returns False off Darwin or on stat error."""
    if sys.platform != "darwin":
        return False
    try:
        st_flags = os.stat(path).st_flags
    except OSError:
        return False
    return bool(st_flags & _SF_DATALESS)


def walk(
    *,
    roots: Iterable[Path],
    includes: list[str] | None = None,
    excludes: list[str] | None = None,
    follow_symlinks: bool = False,
    skip_dirs: frozenset[str] | None = None,
) -> Iterator[Path]:
    """Yield supported files under ``roots`` in deterministic order.

    ``skip_dirs`` is a set of directory basenames pruned at descent — any
    directory whose ``name`` is in the set is not entered. Default is
    :data:`fnd.config.DEFAULT_JUNK_DIRS` so callers that don't pass this
    parameter get the expected developer-junk prune. Pass ``frozenset()``
    to disable the prune entirely (legacy behaviour).
    """
    if skip_dirs is None:
        # Late import: fnd.config imports fnd.walk transitively, so keep
        # this off the module-load path.
        from fnd.config import DEFAULT_JUNK_DIRS

        skip_dirs = DEFAULT_JUNK_DIRS

    suffixes = supported_suffixes()
    inc = list(includes or [])
    exc = list(excludes or [])
    inc_targets_hidden = _glob_targets_hidden(inc)

    for root in roots:
        original = root.expanduser()
        if not follow_symlinks and original.is_symlink():
            # A symlinked root is the only way the index can end up
            # following the link target (the inner symlink-checks below
            # only handle members). Refuse unless the user opted in.
            continue
        try:
            root = original.resolve()
        except OSError:
            continue
        if not root.exists():
            continue
        if root.is_file():
            if root.suffix.lower() in suffixes:
                yield root
            continue

        yield from _scandir_walk(
            root=root,
            suffixes=suffixes,
            inc=inc,
            exc=exc,
            inc_targets_hidden=inc_targets_hidden,
            follow_symlinks=follow_symlinks,
            skip_dirs=skip_dirs,
        )


# Mirrors fnd.migrate._SIDECAR_NAME; kept local so this low-level walker
# doesn't import the higher-level migrate module (which would cycle via index).
_INDEX_SIDECAR = ".fnd-schema-version"


def _is_index_dir(path: str) -> bool:
    """True if ``path`` is an fnd index directory (carries the sidecar)."""
    return os.path.exists(os.path.join(path, _INDEX_SIDECAR))


def _scandir_walk(
    *,
    root: Path,
    suffixes: frozenset[str],
    inc: list[str],
    exc: list[str],
    inc_targets_hidden: bool,
    follow_symlinks: bool,
    skip_dirs: frozenset[str],
) -> Iterator[Path]:
    """DFS via ``os.scandir`` so excluded directories aren't descended.

    Children are sorted by name within each directory so traversal order
    is deterministic and stable across platforms — the legacy ``rglob``
    relied on the filesystem's ordering, which is good enough for the
    indexer but causes flaky tests when the order leaks into assertions.
    """
    # An index directory used directly as a scan root would otherwise have its
    # internals (Tantivy meta.json, the schema sidecar, …) yielded — the
    # per-child guard below only catches index dirs *nested* under the root.
    stack: list[Path] = [] if _is_index_dir(str(root)) else [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = sorted(it, key=lambda e: e.name)
        except (OSError, PermissionError):
            continue

        for entry in entries:
            name = entry.name
            try:
                is_symlink = entry.is_symlink()
                is_dir = entry.is_dir(follow_symlinks=follow_symlinks)
                is_file = entry.is_file(follow_symlinks=follow_symlinks)
            except OSError:
                continue

            if is_dir:
                if not follow_symlinks and is_symlink:
                    continue
                if name in skip_dirs:
                    continue
                # Never descend into an fnd index directory (identified by its
                # schema-version sidecar) — otherwise the walker would index
                # fnd's own internals (e.g. the Tantivy meta.json) when an
                # index lives inside a scanned corpus.
                if _is_index_dir(entry.path):
                    continue
                # Hidden directories pruned by default. Skipping at
                # descent saves walking gigabytes of e.g. ``.git`` on
                # cloned repos even when the user's includes happen to
                # target hidden files for a different reason.
                if name.startswith(".") and not inc_targets_hidden:
                    continue
                stack.append(Path(entry.path))
                continue

            if not is_file:
                continue
            if not follow_symlinks and is_symlink:
                continue
            # Suffix check uses the basename string so we skip Path()
            # allocation for files that can't be indexed anyway — a
            # measurable win on trees with many out-of-scope files
            # (icons, lockfiles, …) sitting next to in-scope ones.
            dot = name.rfind(".")
            if dot < 0 or name[dot:].lower() not in suffixes:
                continue

            entry_path = Path(entry.path)
            try:
                rel = entry_path.relative_to(root)
            except ValueError:
                continue
            # ``as_posix`` (not ``str``) so include/exclude globs — which are
            # always ``/``-delimited — match on Windows, where ``str(Path)``
            # would yield backslash separators and never match.
            rel_str = rel.as_posix()

            # Hidden-file filter mirrors the historical post-rglob check
            # for the file itself; descent-time prune already dropped
            # hidden ancestor directories.
            if _is_hidden(rel) and not inc_targets_hidden:
                continue
            if inc and not _matches_any(inc, rel_str):
                continue
            if exc and _matches_any(exc, rel_str):
                continue

            yield entry_path


def walk_sources(
    *,
    sources: list[SourceConfig],
    skip_dirs: frozenset[str] | None = None,
) -> Iterator[Path]:
    """Yield in-scope paths across every source.

    Per source: applies includes/excludes via :func:`walk`, then on
    ``.md`` files runs the source's frontmatter filter. Frontmatter parse
    errors and missing-field strict-null cases drop the file silently —
    the indexer will eventually log them via ``fnd status --errors``
    (phase 10).

    ``skip_dirs`` is forwarded to :func:`walk`. Indexer entry points
    resolve this from ``defaults.skip_junk_dirs`` + ``extra_junk_dirs``;
    callers that don't pass it inherit the built-in default set.
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
            skip_dirs=skip_dirs,
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

"""Filesystem walker — yield supported files under a collection's roots.

Includes/excludes precedence:

1. A path is in scope only if it lives under one of ``roots``.
2. If ``includes`` is set, the path must match at least one ``includes`` glob.
3. If the path matches **any** ``excludes`` glob, it is dropped — even if it
   matched an ``includes``.
4. Hidden files (``.foo``) are excluded by default. Only an include glob that
   names a dot-prefixed component admits one, and only the paths that glob
   itself matches — ``**/*.md`` alongside it does not widen the exception.
5. Symlinks are followed only if ``follow_symlinks = True``. This applies in
   two places:
   - The collection root itself — if the user-supplied ``root`` is a symlink,
     it is refused unless ``follow_symlinks=True``. This blocks a hostile
     config (or a typo) from pointing fnd at ``/etc`` via a symlinked root.
   - Each file inside the tree — symlinked files are skipped when the flag is
     off. Directory symlinks are not recursed into (we pass
     ``recurse_symlinks=False`` to ``Path.rglob`` rather than relying on the
     Python 3.13 default).

Globs are matched against the path **relative to its root** by
:mod:`fnd.globs`, the same translator the filter DSL's ``~~`` uses — ``*`` and
``?`` stop at ``/``, a whole ``**`` segment spans zero or more directories.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fnd.config import SourceConfig

from fnd.extract import supported_suffixes
from fnd.globs import GlobSet, names_hidden
from fnd.ignore_files import IgnoreStack, ancestor_stack, load_ignore_file


def _is_hidden(rel: Path) -> bool:
    return any(part.startswith(".") for part in rel.parts)


def _hidden_includes(globs: list[str]) -> GlobSet:
    """The include patterns that explicitly name a dot-prefixed component.

    A set, not a flag: one ``.obsidian/**`` beside ``**/*.md`` used to lift the
    hidden prune for the whole tree, so an Obsidian vault indexed every note in
    ``.trash``. Only these globs may admit a hidden path.
    """
    return GlobSet.parse([g for g in globs if names_hidden(g)])


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


def walk(
    *,
    roots: Iterable[Path],
    includes: list[str] | None = None,
    excludes: list[str] | None = None,
    follow_symlinks: bool = False,
    skip_dirs: frozenset[str] | None = None,
    ignore_names: Sequence[str] = (),
    on_unreadable: Callable[[Path], None] | None = None,
) -> Iterator[Path]:
    """Yield supported files under ``roots`` in deterministic order.

    ``skip_dirs`` is a set of directory basenames pruned at descent — any
    directory whose ``name`` is in the set is not entered. Default is
    :data:`fnd.config.DEFAULT_JUNK_DIRS` so callers that don't pass this
    parameter get the expected developer-junk prune. Pass ``frozenset()``
    to disable the prune entirely (legacy behaviour).

    ``ignore_names`` names the ignore files to honour (``.gitignore``,
    ``.fndignore``); empty disables the mechanism entirely.

    ``on_unreadable`` receives each directory below a root that exists but
    cannot be listed: what it holds is unknown, not absent. A root that cannot
    be listed is the caller's question (:func:`fnd.index.unreadable_roots`).
    """
    if skip_dirs is None:
        # Late import: fnd.config imports fnd.walk transitively, so keep
        # this off the module-load path.
        from fnd.config import DEFAULT_JUNK_DIRS

        skip_dirs = DEFAULT_JUNK_DIRS

    suffixes = supported_suffixes()
    inc = GlobSet.parse(includes)
    exc = GlobSet.parse(excludes)
    hidden_inc = _hidden_includes(list(includes or []))

    for root in roots:
        original = root.expanduser()
        # A root that cannot be stat'ed (a locked parent) is skipped here and
        # reported by the caller, like one that cannot be listed.
        try:
            if not follow_symlinks and original.is_symlink():
                # A symlinked root is the only way the index can end up
                # following the link target (the inner symlink-checks below
                # only handle members). Refuse unless the user opted in.
                continue
            root = original.resolve()
            if not root.exists():
                continue
            is_file = root.is_file()
        except OSError:
            continue
        if is_file:
            if root.suffix.lower() in suffixes:
                yield root
            continue

        yield from _scandir_walk(
            root=root,
            suffixes=suffixes,
            inc=inc,
            exc=exc,
            hidden_inc=hidden_inc,
            follow_symlinks=follow_symlinks,
            skip_dirs=skip_dirs,
            ignore_names=ignore_names,
            on_unreadable=on_unreadable,
        )


# Mirrors fnd.migrate._SIDECAR_NAME; kept local so this low-level walker
# doesn't import the higher-level migrate module (which would cycle via index).
_INDEX_SIDECAR = ".fnd-schema-version"


def _is_index_dir(path: str) -> bool:
    """True if ``path`` is an fnd index directory (carries the sidecar)."""
    return os.path.exists(os.path.join(path, _INDEX_SIDECAR))


def _dir_identity(path: Path) -> tuple[int, int] | None:
    """(device, inode) for a directory, or None if it cannot be stat'd.

    Identity rather than the path string: a symlink cycle produces endlessly
    many distinct paths for the same directory, and the walk only terminated
    when the OS refused the depth — after walking one file dozens of times and
    storing the deepest alias as its path.
    """
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


def _scandir_walk(
    *,
    root: Path,
    suffixes: frozenset[str],
    inc: GlobSet,
    exc: GlobSet,
    hidden_inc: GlobSet,
    follow_symlinks: bool,
    skip_dirs: frozenset[str],
    ignore_names: Sequence[str] = (),
    on_unreadable: Callable[[Path], None] | None = None,
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
    # Ignore files apply from the source root downwards; see ancestor_stack.
    base = ancestor_stack(root, ignore_names)
    stack: list[tuple[Path, IgnoreStack]] = [] if _is_index_dir(str(root)) else [(root, base)]
    # Real directories already entered, so a symlink cycle terminates on the
    # first repeat rather than on the OS running out of path. Only needed when
    # following links, and only then does the identity lookup cost anything.
    seen: set[tuple[int, int]] = set()
    while stack:
        current, inherited = stack.pop()
        if follow_symlinks:
            identity = _dir_identity(current)
            if identity is not None:
                if identity in seen:
                    continue
                seen.add(identity)
        try:
            with os.scandir(current) as it:
                entries = sorted(it, key=lambda e: e.name)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            if on_unreadable is not None and current != root:
                on_unreadable(current)
            continue
        # Read this directory's ignore files only when scandir already proved
        # they exist, so a tree without any costs no extra syscalls.
        scope = inherited
        if ignore_names:
            present = {e.name for e in entries}
            # A directory holding .git is a repository root: git applies no
            # outer .gitignore inside it, so neither do we. Nested repos are
            # common in a corpus of cloned assignments. Only git's own files
            # are dropped: a .fndignore is ours and says what the user does
            # not want searched, which a cloned repo has no say over.
            outer = inherited.without(".gitignore") if ".git" in present else inherited
            scope = outer.push(
                *(load_ignore_file(current, n) for n in ignore_names if n in present)
            )

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
                # Descent asks only whether any hidden-targeting glob exists —
                # a glob cannot say whether something under a prefix could match
                # it. The file test below is what decides membership.
                if name.startswith(".") and not hidden_inc:
                    continue
                child = Path(entry.path)
                if scope and scope.ignored(child, is_dir=True):
                    continue
                stack.append((child, scope))
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

            # A hidden path needs a glob that names a dot-prefixed component;
            # ``**/*.md`` matching it is not consent to index ``.trash``.
            if _is_hidden(rel) and not hidden_inc.matches(rel_str):
                continue
            if inc and not inc.matches(rel_str):
                continue
            if exc and exc.matches(rel_str):
                continue
            if scope and scope.ignored(entry_path, is_dir=False):
                continue

            yield entry_path


def _resolved_root(path: Path) -> Path:
    """The root as :func:`walk` sees it, or the original if it cannot resolve."""
    try:
        return path.expanduser().resolve()
    except OSError:
        return path.expanduser()


def walk_sources(
    *,
    sources: list[SourceConfig],
    skip_dirs: frozenset[str] | None = None,
    read_frontmatter: Callable[[Path], dict[str, object] | None] | None = None,
    on_unreadable: Callable[[Path], None] | None = None,
) -> Iterator[Path]:
    """Yield in-scope paths across every source.

    Per source: ``walk`` applies includes/excludes and the ignore files, then
    the source's resolved filters gate each candidate (:mod:`fnd.filters`).
    Frontmatter parse errors and missing-field strict-null cases drop the file
    silently: there is no command that reports them, which is why
    :meth:`IgnoreMatch.describe` exists unused.

    ``skip_dirs`` is forwarded to :func:`walk`. Indexer entry points
    resolve this from ``defaults.skip_junk_dirs`` + ``extra_junk_dirs``;
    callers that don't pass it inherit the built-in default set.

    ``read_frontmatter`` overrides how a candidate's frontmatter is
    obtained. Evaluating the filter means opening the file, which on a
    cloud-backed folder blocks while the provider sends it; the indexer
    substitutes a reader that reports and bounds that wait. Returning
    ``None`` drops the file, so an override can also decline to fetch.
    Defaults to a plain read.

    ``on_unreadable`` is forwarded to :func:`walk`.
    """
    from fnd.config import SourceConfig  # local import: avoid cycle
    from fnd.file_facts import FileFacts
    from fnd.filters import FileGate, build_gate, spec_from_resolved
    from fnd.filters.dimensions import dimension
    from fnd.ignore_files import IGNORE_FILENAMES
    from fnd.tags import TAG_PROVIDERS

    for source in sources:
        assert isinstance(source, SourceConfig)
        resolved = source.effective_filters
        spec = spec_from_resolved(resolved)
        gate = build_gate(spec)
        # Scoped through the dimension rather than by hand: strict null would
        # otherwise fail a frontmatter comparison on every PDF and drop the
        # lot, and a hand-rolled scope here is what let a note with no block
        # through the rule that named its course.
        frontmatter_dim = dimension("frontmatter")
        scoped = [
            rule
            for rule in (
                frontmatter_dim.rule(text)
                for text in (source.legacy_frontmatter, spec.frontmatter)
                if text
            )
            if rule is not None
        ]
        # One gate, not a second `all(...)` beside it: the walk reimplementing
        # the rule combination is how an OR there would go unnoticed.
        gate = FileGate.of(gate.rules + tuple(scoped))
        names = [
            name
            for name, on in (
                (".gitignore", resolved.respect_gitignore),
                (".fndignore", resolved.respect_fndignore),
            )
            if on and name in IGNORE_FILENAMES
        ]
        providers = [p for p in TAG_PROVIDERS.values() if p.available_on(sys.platform)]
        # ``walk`` resolves the root before yielding, so facts must measure
        # against the resolved form. macOS /tmp and /var are themselves
        # symlinks, so a mismatch here silently turns ``file.path`` into an
        # absolute path and any rule using it stops matching.
        facts_root = _resolved_root(source.path)
        for path in walk(
            roots=[source.path],
            includes=source.includes or None,
            excludes=source.excludes or None,
            follow_symlinks=source.follow_symlinks,
            skip_dirs=skip_dirs,
            ignore_names=names,
            on_unreadable=on_unreadable,
        ):
            if not gate:
                yield path
                continue
            facts = FileFacts(
                path,
                root=facts_root,
                read_frontmatter=read_frontmatter,
                tag_providers=providers,
            )
            if gate.passes(facts):
                yield path

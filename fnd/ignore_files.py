"""``.gitignore`` / ``.fndignore`` matching.

Git's own semantics, hand-rolled over the shared translator in
:mod:`fnd.globs`: this module owns the *policy* — per-directory stacking,
innermost-wins, negation, anchoring, case-folding — while the pattern language
itself is shared with config globs. Correctness is held by differential tests
against ``git check-ignore``.

Two rules are load-bearing and easy to lose:

* Each file is anchored to *its own* directory, not the source root. A repo
  can sit above or below the indexed root.
* An excluded directory is never descended into, so a negation cannot
  re-include anything beneath it. That is git's rule, not an optimisation.
* A directory holding ``.git`` is a repository root, and an outer
  ``.gitignore`` does not reach inside it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from fnd.globs import translate

__all__ = [
    "IGNORE_FILENAMES",
    "IgnoreFile",
    "IgnoreMatch",
    "IgnoreStack",
    "Pattern",
    "ancestor_stack",
    "load_ignore_file",
    "parse_patterns",
]

IGNORE_FILENAMES: Final = (".gitignore", ".fndignore")

# Bounds an ignore file read; a pathological file must not stall a scan.
_MAX_BYTES: Final = 1 << 20
_MAX_PATTERN_LEN: Final = 4096


@dataclass(frozen=True, slots=True)
class Pattern:
    regex: re.Pattern[str]
    negated: bool
    dir_only: bool
    source: str
    lineno: int
    fold_case: bool = False
    """Match against a lowercased path, as git does under core.ignorecase."""


@dataclass(frozen=True, slots=True)
class IgnoreMatch:
    """Which pattern decided, so a preview can explain itself."""

    pattern: Pattern
    origin: Path

    @property
    def ignored(self) -> bool:
        return not self.pattern.negated

    def describe(self) -> str:
        return f"{self.origin}:{self.pattern.lineno}:{self.pattern.source}"


def _strip_trailing_space(line: str) -> str:
    """Trailing whitespace is insignificant unless backslash-escaped."""
    out = line
    while out.endswith((" ", "\t")):
        stripped = out[:-1]
        if stripped.endswith("\\"):
            break
        out = stripped
    return out


def _fold_case(directory: Path, name: str) -> bool:
    """Whether this filesystem is case-insensitive, asked of it directly.

    git sets ``core.ignorecase`` from the filesystem at clone time, so a
    case-sensitive volume on macOS gets case-sensitive matching. Keying off
    ``sys.platform`` instead would exclude files git keeps — the same class of
    silent over-exclusion, in the other direction.
    """
    flipped = name.upper() if name != name.upper() else name.lower()
    if flipped == name:
        return False
    try:
        return (directory / flipped).exists()
    except OSError:
        return False


def parse_patterns(text: str, *, fold_case: bool = False) -> tuple[Pattern, ...]:
    """One :class:`Pattern` per significant line, in file order.

    ``fold_case`` matches git's ``core.ignorecase``; see :func:`_fold_case`.
    """
    out: list[Pattern] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = _strip_trailing_space(raw)
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        source = line
        negated = line.startswith("!")
        if negated or line.startswith("\\"):
            line = line[1:]
        if not line:
            continue
        dir_only = line.endswith("/")
        if dir_only:
            line = line[:-1]
        if not line or len(line) > _MAX_PATTERN_LEN:
            continue
        # A slash anywhere but the (already stripped) end anchors the pattern
        # to the ignore file's own directory.
        anchored = "/" in line
        if line.startswith("/"):
            line = line[1:]
        if not line:
            continue
        try:
            regex = translate(line, anchored=anchored, fold_case=fold_case)

        except re.error:
            # git tolerates a pattern its own matcher cannot use — an inverted
            # character range, say — by never matching it. Aborting the whole
            # index run over one line of one .gitignore does not.
            continue
        out.append(
            Pattern(
                regex=regex,
                negated=negated,
                dir_only=dir_only,
                source=source,
                lineno=lineno,
                fold_case=fold_case,
            )
        )
    return tuple(out)


@dataclass(frozen=True, slots=True)
class IgnoreFile:
    path: Path
    anchor: Path
    patterns: tuple[Pattern, ...]

    def match(self, target: Path, *, is_dir: bool) -> IgnoreMatch | None:
        try:
            rel = target.relative_to(self.anchor).as_posix()
        except ValueError:
            return None
        if not rel or rel == ".":
            return None
        decided: Pattern | None = None
        for pattern in self.patterns:
            if pattern.dir_only and not is_dir:
                continue
            if pattern.regex.match(rel.lower() if pattern.fold_case else rel):
                decided = pattern  # last match in a file wins
        return IgnoreMatch(decided, self.path) if decided is not None else None


def load_ignore_file(directory: Path, name: str) -> IgnoreFile | None:
    """Read one ignore file, or ``None`` when absent or unreadable."""
    path = directory / name
    try:
        raw = path.read_bytes()[:_MAX_BYTES]
    except OSError:
        return None
    text = raw.decode("utf-8", errors="replace")
    patterns = parse_patterns(text, fold_case=_fold_case(directory, name))
    return IgnoreFile(path=path, anchor=directory, patterns=patterns) if patterns else None


@dataclass(frozen=True, slots=True)
class IgnoreStack:
    """Ignore files in scope, outermost first.

    Immutable so the walker can carry one per stack entry — its DFS holds
    siblings from several parents at once, and a mutable push/pop would apply
    a sibling's rules to the wrong subtree.
    """

    files: tuple[IgnoreFile, ...] = ()

    def push(self, *added: IgnoreFile | None) -> IgnoreStack:
        real = tuple(f for f in added if f is not None)
        return IgnoreStack(self.files + real) if real else self

    def match(self, target: Path, *, is_dir: bool) -> IgnoreMatch | None:
        decided: IgnoreMatch | None = None
        for ignore_file in self.files:
            found = ignore_file.match(target, is_dir=is_dir)
            if found is not None:
                decided = found  # innermost file wins
        return decided

    def ignored(self, target: Path, *, is_dir: bool) -> bool:
        found = self.match(target, is_dir=is_dir)
        return found is not None and found.ignored

    def __bool__(self) -> bool:
        return bool(self.files)


# Climbing stops here; a pathological path must not walk to the filesystem root.
def ancestor_stack(_root: Path, _names: Sequence[str]) -> IgnoreStack:
    """Always empty: ignore files apply from the source root downwards.

    git would apply an enclosing repository's rules to a subdirectory, but a
    source is a folder the user named, and honouring rules written for a
    different purpose above it is destructive out of proportion to the case it
    serves. A dotfiles repository in the home directory — ``*`` plus a handful
    of negations, a common shape — makes every file under ``~/Documents``
    ignored, and the source indexes nothing at all.
    """
    return IgnoreStack()

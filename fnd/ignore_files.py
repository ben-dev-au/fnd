"""``.gitignore`` / ``.fndignore`` matching.

Git's own semantics, hand-rolled: pattern translation is small and the format
is frozen, while the part that actually decides an answer — per-directory
stacking, innermost-wins, negation — is ours either way. Correctness is held
by differential tests against ``git check-ignore``.

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
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

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


def _class_span(pattern: str, i: int) -> int:
    """Index just past a ``[...]`` class starting at ``i``, or ``i`` if unclosed."""
    j = i + 1
    if j < len(pattern) and pattern[j] in ("!", "^"):
        j += 1
    if j < len(pattern) and pattern[j] == "]":
        j += 1
    while j < len(pattern) and pattern[j] != "]":
        j += 1
    return j + 1 if j < len(pattern) else i


def _translate_segment(segment: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(segment):
        ch = segment[i]
        if ch == "*":
            # Collapse a run of stars into one. Consecutive ``[^/]*`` groups
            # mean the same thing but backtrack exponentially, so a hostile
            # pattern in any cloned repo's .gitignore would hang the scan.
            while i < len(segment) and segment[i] == "*":
                i += 1
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "[":
            end = _class_span(segment, i)
            if end == i:
                out.append(re.escape("["))
                i += 1
                continue
            body = segment[i + 1 : end - 1]
            if body.startswith(("!", "^")):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            i = end
        elif ch == "\\" and i + 1 < len(segment):
            out.append(re.escape(segment[i + 1]))
            i += 2
        else:
            out.append(re.escape(ch))
            i += 1
    return "".join(out)


def _collapse(segments: list[str]) -> list[str]:
    """Drop a repeated ``**`` segment. ``**/**/x`` means ``**/x``, but each
    one compiles to its own unbounded group and they backtrack together."""
    out: list[str] = []
    for segment in segments:
        if segment == "**" and out and out[-1] == "**":
            continue
        out.append(segment)
    return out


def _translate(pattern: str, *, anchored: bool) -> re.Pattern[str]:
    segments = _collapse(pattern.split("/"))
    parts: list[str] = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            # Trailing ``/**`` matches everything below; elsewhere it spans
            # zero or more directories.
            parts.append("(?:.*)" if last else "(?:[^/]+/)*")
            continue
        parts.append(_translate_segment(segment))
        if not last:
            parts.append("/")
    body = "".join(parts)
    prefix = "" if anchored else "(?:.*/)?"
    # Matches the path itself only. A pattern naming a directory covers its
    # contents because the walker never descends into an ignored directory —
    # extending the regex over descendants instead would let a negated
    # pattern re-include files the following patterns should still exclude.
    # git sets core.ignorecase on a case-insensitive filesystem, which is the
    # default on macOS and Windows, and then ignores README.md for a
    # "readme.md" rule. Matching case-sensitively there disagrees with the
    # tool whose semantics this implements.
    flags = re.IGNORECASE if sys.platform in ("darwin", "win32") else 0
    return re.compile(f"^{prefix}{body}$", flags)


def parse_patterns(text: str) -> tuple[Pattern, ...]:
    """One :class:`Pattern` per significant line, in file order."""
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
            regex = _translate(line, anchored=anchored)
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
            if pattern.regex.match(rel):
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
    patterns = parse_patterns(text)
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
def ancestor_stack(root: Path, names: Sequence[str]) -> IgnoreStack:
    """Always empty: ignore files apply from the source root downwards.

    git would apply an enclosing repository's rules to a subdirectory, but a
    source is a folder the user named, and honouring rules written for a
    different purpose above it is destructive out of proportion to the case it
    serves. A dotfiles repository in the home directory — ``*`` plus a handful
    of negations, a common shape — makes every file under ``~/Documents``
    ignored, and the source indexes nothing at all.
    """
    return IgnoreStack()

"""The glob language shared by config globs and ignore files.

One translation core, two policies over it: ignore files add git's anchoring,
negation, directory-only and case-folding rules; a config glob is the anchored,
case-sensitive case. Sharing the core is what stops ``walk``'s include/exclude
globs and the filter DSL's ``~~`` from answering differently about one path —
they were separate ``fnmatch`` call sites and disagreed on every ``**/``-prefixed
pattern.

``*`` and ``?`` stop at ``/``; a whole ``**`` segment spans zero or more
directories. A pattern the regex engine cannot compile never matches, rather
than aborting the scan that used it.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Iterable
from dataclasses import dataclass

__all__ = ["GlobSet", "PathGlob", "translate"]


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


def _translate_segment(segment: str, *, fold_case: bool = False) -> str:
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
            nxt = segment[i + 1]
            out.append(re.escape(nxt.lower() if fold_case else nxt))
            i += 2
        else:
            out.append(re.escape(ch.lower() if fold_case else ch))
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


def translate(pattern: str, *, anchored: bool, fold_case: bool = False) -> re.Pattern[str]:
    """Compile one glob. ``anchored`` fixes it to the root; otherwise it may
    start at any directory, which is git's rule for a slashless pattern."""
    segments = _collapse(pattern.split("/"))
    parts: list[str] = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            # Trailing ``/**`` matches everything below; elsewhere it spans
            # zero or more directories.
            parts.append("(?:.*)" if last else "(?:[^/]+/)*")
            continue
        parts.append(_translate_segment(segment, fold_case=fold_case))
        if not last:
            parts.append("/")
    body = "".join(parts)
    prefix = "" if anchored else "(?:.*/)?"
    # Matches the path itself only. A pattern naming a directory covers its
    # contents because the walker never descends into an ignored directory —
    # extending the regex over descendants instead would let a negated
    # pattern re-include files the following patterns should still exclude.
    # No re.IGNORECASE: that folds a character class too, so "*.[CH]" would
    # match "x.c" where git keeps it. The literals are lowered above and the
    # path is lowered at match time, which is what git's WM_CASEFOLD does.
    return re.compile(f"^{prefix}{body}$")


@functools.lru_cache(maxsize=2048)
def _config_regex(pattern: str) -> re.Pattern[str] | None:
    try:
        return translate(pattern, anchored=True)
    except re.error:
        return None


@dataclass(frozen=True, slots=True)
class PathGlob:
    """One config glob, matched case-sensitively against a root-relative,
    ``/``-delimited path. Compilation is memoised, so holding the pattern
    string is enough to be cheap in a per-file loop."""

    pattern: str

    def matches(self, rel: str) -> bool:
        regex = _config_regex(self.pattern)
        return regex is not None and regex.match(rel) is not None


@dataclass(frozen=True, slots=True)
class GlobSet:
    """An includes or excludes list. Falsey when empty, because an empty
    include list means *everything* and must not be tested."""

    globs: tuple[PathGlob, ...] = ()

    @classmethod
    def parse(cls, patterns: Iterable[str] | None) -> GlobSet:
        return cls(tuple(PathGlob(p) for p in patterns or ()))

    def __bool__(self) -> bool:
        return bool(self.globs)

    def matches(self, rel: str) -> bool:
        return any(g.matches(rel) for g in self.globs)

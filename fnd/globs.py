"""The glob language shared by config globs and ignore files.

One translation core, two policies over it: ignore files add git's anchoring,
negation, directory-only and case-folding rules; a config glob is the anchored,
case-sensitive case. Sharing the core is what stops ``walk``'s include/exclude
globs and the filter DSL's ``~~`` from answering differently about one path, as
separate ``fnmatch`` call sites do on every ``**/``-prefixed pattern.

``*``, ``?`` and a character class all stop at ``/``; a whole ``**`` segment
spans zero or more directories. A pattern the regex engine cannot compile never
matches, rather than aborting the scan that used it.

Each segment compiles so the engine has a single path to try; see
:func:`_translate_segment` for why, and for the one place that would be unsound.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Iterable
from dataclasses import dataclass

__all__ = ["GlobSet", "PathGlob", "names_hidden", "translate"]


def names_hidden(pattern: str) -> bool:
    """True when a glob names a dot-prefixed path component, which is what lets
    an include glob admit a hidden file."""
    return any(part.startswith(".") for part in pattern.split("/"))


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


def _segment_tokens(segment: str, *, fold_case: bool) -> list[str | None]:
    """Regex source per glob token, with ``None`` marking a ``*``."""
    out: list[str | None] = []
    i = 0
    while i < len(segment):
        ch = segment[i]
        if ch == "*":
            # A run of stars is one star. Consecutive groups mean the same
            # thing but multiply the search space.
            while i < len(segment) and segment[i] == "*":
                i += 1
            out.append(None)
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
            # A class never matches the separator, as `?` never does: git
            # ignores "bb" for `*[!a]*[!a]` but not "b/b", and `[^a]` alone
            # would match the slash and let one segment span two.
            out.append("(?=[^/])[" + body.replace("\\", "\\\\") + "]")
            i = end
        elif ch == "\\" and i + 1 < len(segment):
            nxt = segment[i + 1]
            out.append(re.escape(nxt.lower() if fold_case else nxt))
            i += 2
        else:
            out.append(re.escape(ch.lower() if fold_case else ch))
            i += 1
    return out


def _translate_segment(segment: str, *, fold_case: bool = False) -> str:
    """One path segment, compiled so the engine has a single path to try.

    ``[^/]*a[^/]*b`` lets the engine partition the text between the two stars
    in exponentially many ways, and it tries them all before failing: five
    stars against a 255-character name does not finish. Each star whose run is
    followed by another star becomes a tempered token, ``(?:(?!run)[^/])*run``,
    which can only reach the FIRST occurrence of that run, so there is one path
    and matching is linear.

    Tempering is sound exactly where it is applied. Taking the leftmost run
    leaves more text for the rest of the pattern, and a star follows, which can
    absorb it. The final run has no star after it and is pinned to the end of
    the segment, so it stays a plain scan: ``*a`` must still match ``aa``.
    """
    tokens = _segment_tokens(segment, fold_case=fold_case)
    runs: list[list[str]] = [[]]
    stars: list[bool] = []
    for token in tokens:
        if token is None:
            stars.append(True)
            runs.append([])
        else:
            runs[-1].append(token)

    out: list[str] = ["".join(runs[0])]
    for index in range(len(stars)):
        run = "".join(runs[index + 1])
        followed_by_star = index + 1 < len(stars)
        if run and followed_by_star:
            out.append(f"(?:(?!{run})[^/])*{run}")
        else:
            out.append(f"[^/]*{run}")
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
    # The path itself only: the walker skips ignored directories, and covering
    # descendants would let a negation re-include what later patterns exclude.
    # No re.IGNORECASE, which folds "[CH]" where git does not; \Z, as $ matches before "\n".
    return re.compile(f"^{prefix}{body}\\Z")


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

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
A pattern with several unbounded ``**`` runs is matched a component at a time
instead; see :class:`_ComponentMatcher`.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from fnd.text_canon import canonical

__all__ = ["CompiledGlob", "GlobSet", "PathGlob", "config_regex", "names_hidden", "translate"]


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


@dataclass(frozen=True, slots=True)
class _ComponentMatcher:
    """A glob with several unbounded runs, matched component by component.

    As one regex, ten ``**`` try about C(60, 10) splits of a 60-deep path; here
    each segment meets each component once, answering as the regex would.
    """

    inner: tuple[re.Pattern[str] | None, ...]
    """Every segment but the last; ``None`` is a ``**`` spanning directories."""
    final: re.Pattern[str] | None
    """``None`` for a trailing ``/**``."""
    anchored: bool

    def match(self, text: str) -> Literal[True] | None:
        """``None`` on a miss, as :meth:`re.Pattern.match` returns."""
        parts = text.split("/")
        last = len(parts) - 1
        at = [0]  # ascending component indices the next segment may start at
        if not self.anchored:
            at = []
            for start, part in enumerate(parts):
                at.append(start)
                if "\n" in part:
                    break
        for segment in self.inner:
            if segment is None:
                starts = set(at)
                spread: list[int] = []
                for k in range(at[0], last + 1):
                    if k in starts or (spread and spread[-1] == k - 1 and parts[k - 1]):
                        spread.append(k)
                at = spread
            else:
                at = [k + 1 for k in at if k < last and segment.fullmatch(parts[k])]
            if not at:
                return None
        if self.final is None:
            matched = "\n" not in "/".join(parts[at[-1] :])
        else:
            matched = at[-1] == last and self.final.fullmatch(parts[last]) is not None
        return True if matched else None


CompiledGlob = re.Pattern[str] | _ComponentMatcher


def translate(pattern: str, *, anchored: bool, fold_case: bool = False) -> CompiledGlob:
    """Compile one glob. ``anchored`` fixes it to the root; otherwise it may
    start at any directory, which is git's rule for a slashless pattern.
    The pattern is canonical, so match it against :func:`canonical` text."""
    segments = _collapse(canonical(pattern).split("/"))
    # One unbounded run backtracks over the path's depth alone; two multiply.
    if segments[:-1].count("**") + (0 if anchored else 1) > 1:
        compiled = [
            None if s == "**" else re.compile(_translate_segment(s, fold_case=fold_case))
            for s in segments
        ]
        return _ComponentMatcher(tuple(compiled[:-1]), compiled[-1], anchored)
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
def config_regex(pattern: str) -> CompiledGlob | None:
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
        return self.matches_canonical(canonical(rel))

    def matches_canonical(self, rel: str) -> bool:
        """:meth:`matches` for a path already :func:`canonical`."""
        regex = config_regex(self.pattern)
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
        rel = canonical(rel)
        return any(g.matches_canonical(rel) for g in self.globs)

    def covers_dir(self, rel: str) -> bool:
        """Whether every path under folder ``rel`` matches: a ``prefix/**`` glob whose prefix does."""
        return any(
            g.pattern.endswith("/**")
            and len(g.pattern) > 3
            and PathGlob(g.pattern[:-3]).matches(rel)
            for g in self.globs
        )

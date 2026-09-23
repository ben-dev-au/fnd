"""Bringing a config file up to the current shape, once, on disk.

Each entry moves a config one version forward. A migration exists so a legacy
shape can be *retired* rather than honoured forever: once every config on disk
is at version N, the compatibility code for anything older can go.

Two kinds of step:

* A shape the models already normalise on load; a legacy key promoted by a
  validator; needs no transform here. Loading and re-rendering persists the
  normalised form, so the entry is a no-op with a note saying why.
* A shape the models cannot read at all, such as a key renamed out from under
  them, needs a real transform over the raw mapping, before validation.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable, MutableMapping
from enum import Enum
from itertools import pairwise, product
from typing import Any, Final

from fnd.globs import names_hidden

#: What this build writes. Bump when adding a migration.
CONFIG_VERSION: Final = 2

VERSION_KEY: Final = "config_version"

Transform = Callable[[MutableMapping[str, Any]], list[str] | None]
"""Edits the raw mapping in place; returns anything the user must be told."""


def _to_v1(raw: MutableMapping[str, Any]) -> None:
    """Retire ``frontmatter_filter`` into the source's filters table.

    Flat ``roots`` and complete sets of type globs in ``includes`` are promoted
    by model validators, so re-rendering persists them unaided. This one is not:
    the model keeps the legacy field as it found it, so without an explicit move
    the rule would be carried forever, or dropped.
    """
    collections = raw.get("collections")
    if not isinstance(collections, dict):
        return
    for collection in collections.values():
        if not isinstance(collection, dict):
            continue
        for source in collection.get("sources") or ():
            if not isinstance(source, dict):
                continue
            legacy = source.pop("frontmatter_filter", None)
            if not legacy:
                continue
            filters = source.setdefault("filters", {})
            if isinstance(filters, dict):
                filters.setdefault("frontmatter", legacy)


def _to_v2(raw: MutableMapping[str, Any]) -> list[str]:
    """Respell each ``fnmatch`` glob as path globs that select what it did.

    ``fnmatch``'s ``*``, ``?`` and negated class cross ``/``, and the legacy walk
    also tried a ``**/`` glob unprefixed against a root-level file. A path glob
    stops at a separator, so ``notes/*.md`` left as written would drop
    ``notes/sub/x.md`` from the index on the next update. Collection-level globs
    count too: the flat legacy shape promotes them into its source.
    """
    narrowed: list[str] = []
    for collection in (raw.get("collections") or {}).values():
        if not isinstance(collection, dict):
            continue
        tables = [collection, *(s for s in collection.get("sources") or () if isinstance(s, dict))]
        for table in tables:
            for key in ("includes", "excludes"):
                globs = table.get(key)
                if isinstance(globs, list):
                    table[key] = _respelled(globs, include=key == "includes", narrowed=narrowed)
    return [
        f"Glob {glob!r} may now match fewer files: spelling out every way its "
        f"wildcards could span folders took over {_MAX_SPELLINGS} globs"
        for glob in dict.fromkeys(narrowed)
    ]


_MAX_SPELLINGS: Final = 16
"""Path globs one legacy glob may become. Each ``?`` roughly doubles the count."""


def _respelled(globs: list[Any], *, include: bool, narrowed: list[str]) -> list[Any]:
    """Each glob respelled, in order and without repeats. An include list naming
    no hidden component never admitted a hidden file, so a spelling that names
    one (``a?.md`` as ``a/.md``) is dropped rather than lift the hidden prune."""
    admits_hidden = not include or any(isinstance(g, str) and names_hidden(g) for g in globs)
    out: list[Any] = []
    for glob in globs:
        spellings: list[Any] = [glob]
        if isinstance(glob, str):
            spellings, exact = _path_globs(glob)
            if not exact:
                narrowed.append(glob)
        for spelling in spellings:
            hidden = isinstance(spelling, str) and names_hidden(spelling)
            if (admits_hidden or not hidden) and spelling not in out:
                out.append(spelling)
    return out


class _Mark(Enum):
    STAR = "*"
    SEP = "/"


_Piece = str | _Mark


def _path_globs(glob: str) -> tuple[list[str], bool]:
    """The path globs whose union matches the paths ``glob`` did, and whether
    exactly: past :data:`_MAX_SPELLINGS`, a ``?`` or class stops standing for
    ``/``, then a star inside a segment stops spanning folders."""
    legacy = _legacy_pieces(glob)
    within = [tuple(o for o in p if o is not _Mark.SEP) or p for p in legacy]
    out: list[str] = []
    for pieces, stars_cross in ((legacy, True), (within, True), (within, False)):
        out = _spellings(pieces, stars_cross=stars_cross)
        if glob.startswith("**/"):
            _add_root_readings(out, _legacy_pieces(glob[3:]))
        if len(out) <= _MAX_SPELLINGS:
            return out or [_matching_nothing(glob)], pieces is legacy
    return out, False


def _matching_nothing(glob: str) -> str:
    """A glob that matched nothing, given the empty segment no path has."""
    empty_segment = glob.startswith("/") or glob.endswith("/") or "//" in glob
    return glob if empty_segment else f"/{glob}"


def _add_root_readings(out: list[str], rest: list[tuple[_Piece, ...]]) -> None:
    """The legacy walk also matched ``**/rest`` as ``rest`` against a root-level
    file. Beside ``*/**/rest`` that reading makes ``**/rest``."""
    for flat in product(*(tuple(o for o in p if o is not _Mark.SEP) for p in rest)):
        if not flat:
            continue
        pieces = [(_Mark.STAR,), (_Mark.SEP,), *((p,) for p in flat)]
        below = _spellings(pieces, stars_cross=False)[0]
        at_root = "".join(p.value if isinstance(p, _Mark) else p for p in flat)
        if below in out:
            out[out.index(below)] = f"**/{at_root}"
        elif at_root not in out:
            out.append(at_root)


def _legacy_pieces(glob: str) -> list[tuple[_Piece, ...]]:
    """Each ``fnmatch`` token of ``glob``, as the pieces it may stand for."""
    out: list[tuple[_Piece, ...]] = []
    i, n = 0, len(glob)
    while i < n:
        c = glob[i]
        i += 1
        if c == "*":
            if not out or out[-1] != (_Mark.STAR,):
                out.append((_Mark.STAR,))
        elif c == "?":
            out.append(("?", _Mark.SEP))
        elif c == "/":
            out.append((_Mark.SEP,))
        elif c == "[" and (end := _legacy_class_end(glob, i)) is not None:
            out.append(_legacy_class(glob[i - 1 : end + 1]))
            i = end + 1
        else:
            out.append((f"\\{c}" if c in "*?[\\" else c,))
    return out


def _legacy_class_end(glob: str, start: int) -> int | None:
    """Index of the ``]`` closing a class whose body starts at ``start``, the
    way ``fnmatch`` finds it."""
    j = start
    if j < len(glob) and glob[j] == "!":
        j += 1
    if j < len(glob) and glob[j] == "]":
        j += 1
    while j < len(glob) and glob[j] != "]":
        j += 1
    return j if j < len(glob) else None


def _legacy_class(token: str) -> tuple[_Piece, ...]:
    """A class as its spellings within a segment, plus a separator if it admits
    ``/``. Read from ``fnmatch``'s own regex, which settles its reading of
    ranges, escapes and a leading ``^``."""
    source = fnmatch.translate(token)
    crosses = re.match(source, "/") is not None
    body = source.removeprefix("(?s:").rpartition(")")[0]
    if body == "(?!)":
        spelled: list[str] = []
    elif body == ".":
        spelled = ["?"]
    else:
        negated = body.startswith("[^")
        spans = _without_separator(_class_spans(body[2 if negated else 1 : -1]))
        spelled = _class_spellings(spans, negated=negated)
    return (*spelled, *((_Mark.SEP,) if crosses else ()))


def _class_spans(inner: str) -> list[tuple[int, int]]:
    """The code-point ranges of a regex class body, read as ``re`` reads it."""
    atoms: list[tuple[str, bool]] = []
    k = 0
    while k < len(inner):
        escaped = inner[k] == "\\"
        atoms.append((inner[k + 1] if escaped else inner[k], escaped))
        k += 2 if escaped else 1
    spans: list[tuple[int, int]] = []
    k = 0
    while k < len(atoms):
        lo = atoms[k][0]
        if k + 2 < len(atoms) and atoms[k + 1] == ("-", False):
            spans.append((ord(lo), ord(atoms[k + 2][0])))
            k += 3
        else:
            spans.append((ord(lo), ord(lo)))
            k += 1
    return spans


def _without_separator(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sorted, merged spans with ``/`` cut out: a path glob's class never matches
    it, and the text of one cannot hold it."""
    slash = ord("/")
    merged: list[tuple[int, int]] = []
    for lo, hi in sorted(spans):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
        else:
            merged.append((lo, hi))
    out: list[tuple[int, int]] = []
    for lo, hi in merged:
        out += [s for s in ((lo, min(hi, slash - 1)), (max(lo, slash + 1), hi)) if s[0] <= s[1]]
    return out


def _class_spellings(spans: list[tuple[int, int]], *, negated: bool) -> list[str]:
    """Path-glob text for a class. ``]`` must lead, ``-`` trail, and a leading
    ``!``, ``^`` or ``[`` would read as syntax; a class of only those three
    cannot be spelled, so each member becomes its own literal."""
    if not spans:
        return ["?"] if negated else []
    lead: list[str] = []
    trail: list[str] = []
    body: list[tuple[int, int]] = []
    for lo, hi in spans:
        for edge, bucket in ((ord("]"), lead), (ord("-"), trail)):
            if lo == edge:
                bucket.append(chr(edge))
                lo += 1
            elif hi == edge:
                bucket.append(chr(edge))
                hi -= 1
        if lo <= hi:
            body.append((lo, hi))
    if not negated and not lead:
        safe = [s for s in body if chr(s[0]) not in "!^["]
        if safe:
            body = [safe[0], *(s for s in body if s is not safe[0])]
        elif trail:
            lead, trail = trail, []
        elif wide := next((s for s in body if s[0] < s[1]), None):
            body = [(wide[0] + 1, wide[1]), *(s for s in body if s is not wide), (wide[0], wide[0])]
        else:
            return [f"\\{chr(lo)}" if chr(lo) == "[" else chr(lo) for lo, _ in body]
    text = "".join(chr(lo) if lo == hi else f"{chr(lo)}-{chr(hi)}" for lo, hi in body)
    return [f"[{'!' if negated else ''}{''.join(lead)}{text}{''.join(trail)}]"]


def _star_spellings(combo: tuple[_Piece, ...], k: int, *, cross: bool) -> tuple[str, ...]:
    """A star within its segment and, if ``cross``, across segments; one glob
    where it sits at a segment edge, which holds because no path has an empty
    segment."""
    opens = k == 0 or combo[k - 1] is _Mark.SEP
    after = combo[k + 1] if k + 1 < len(combo) else None
    if after is None:
        spelled = ("**",) if opens else ("*", "*/**")
    elif after is _Mark.SEP:
        spelled = ("*/**",)
    else:
        spelled = ("**/*",) if opens else ("*", "*/**/*")
    return spelled if cross else spelled[:1]


def _spellings(pieces: list[tuple[_Piece, ...]], *, stars_cross: bool) -> list[str]:
    """Readings of ``pieces`` as path globs, the one keeping every wildcard in
    its segment first. Stops once past :data:`_MAX_SPELLINGS`."""
    out: dict[str, None] = {}
    for combo in product(*pieces):
        seps = [p is _Mark.SEP for p in combo]
        if seps and (seps[0] or seps[-1] or any(a and b for a, b in pairwise(seps))):
            continue
        options = [
            _star_spellings(combo, k, cross=stars_cross)
            if p is _Mark.STAR
            else ("/" if p is _Mark.SEP else p,)
            for k, p in enumerate(combo)
        ]
        for parts in product(*options):
            out[_collapse_globstars("".join(parts))] = None
            if len(out) > _MAX_SPELLINGS:
                return list(out)
    return list(out)


def _collapse_globstars(glob: str) -> str:
    segments: list[str] = []
    for segment in glob.split("/"):
        if not (segment == "**" and segments and segments[-1] == "**"):
            segments.append(segment)
    return "/".join(segments)


MIGRATIONS: Final[tuple[tuple[int, str, Transform], ...]] = (
    (1, "Adopt the canonical layout and record a config version", _to_v1),
    (2, "Respell globs so they still select the files they did", _to_v2),
)


class ConfigTooNewError(Exception):
    """A config written by a newer fnd. Loading it would silently drop keys."""

    def __init__(self, found: int, supported: int) -> None:
        super().__init__(
            f"config_version = {found}, but this fnd understands up to "
            f"{supported}. Update fnd, or restore the backup written beside "
            f"the config when it was last migrated."
        )
        self.found = found
        self.supported = supported

    def __reduce__(self) -> tuple[Any, tuple[int, int]]:
        return (ConfigTooNewError, (self.found, self.supported))


def version_of(raw: MutableMapping[str, Any]) -> int:
    """The version a raw config declares. Absent means pre-versioning."""
    value = raw.get(VERSION_KEY, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def check_version(raw: MutableMapping[str, Any]) -> int:
    """Refuse a config from a newer fnd, and consume the key so it never
    reaches a model that forbids unknown fields. Applies no transform."""
    found = version_of(raw)
    if found > CONFIG_VERSION:
        raise ConfigTooNewError(found, CONFIG_VERSION)
    raw.pop(VERSION_KEY, None)
    return found


def migrate(raw: MutableMapping[str, Any]) -> tuple[int, list[str]]:
    """Apply every outstanding step in place. Returns the version reached and
    what was done, so the caller can report it."""
    found = check_version(raw)
    applied: list[str] = []
    for target, description, transform in MIGRATIONS:
        if found < target:
            notes = transform(raw)
            applied += [description, *(notes or ())]
    return CONFIG_VERSION, applied

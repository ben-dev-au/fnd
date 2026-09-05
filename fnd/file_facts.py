"""Lazy per-file attributes for index-time filtering.

A :class:`Mapping` so :mod:`fnd.filter_dsl` predicates evaluate against it
unchanged. Reserved ``file.*`` keys are computed on first access and cached;
every other key falls through to the file's frontmatter. The two namespaces
cannot collide: a frontmatter key can never contain a dot
(``fnd.frontmatter._KEY_VALUE``).

Caching is load-bearing, not an optimisation — ``Mapping.__contains__`` falls
through to ``__getitem__``, so the evaluator's ``field not in fm`` followed by
``fm[field]`` reads every key twice.

``__getitem__`` raises only ``KeyError``. A fact that cannot be determined
(no birth time on ext4, an unreadable xattr) is *unknown* rather than absent:
both raise, but :meth:`FileFacts.is_unknown` distinguishes them so a rule can
decide whether unknown means pass or drop. Nothing else may raise — a
predicate is documented as pure, and an escaping error would abort the whole
index run on one malformed file.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Final

from fnd.frontmatter import FrontmatterParseError, read_frontmatter_from_file
from fnd.fsmeta import read_file_times
from fnd.kinds import KIND_BY_ID, kind_for_suffix
from fnd.tags import TagContext, TagProvider, read_tags

__all__ = ["RESERVED_FACTS", "FileFacts", "is_fact_name"]

_PREFIX: Final = "file."

# Every reserved key. Anything else dotted is a typo, not a frontmatter key,
# so callers can reject it at parse time instead of strict-nulling to False.
RESERVED_FACTS: Final[frozenset[str]] = frozenset(
    {
        "file.path",
        "file.name",
        "file.ext",
        "file.kind",
        "file.category",
        "file.size",
        "file.created",
        "file.modified",
        "file.hidden",
        "file.tags.os",
        "file.tags.frontmatter",
        "file.tags.all",
    }
)

# Kinds that can carry a YAML frontmatter block. Imported lazily-ish here
# rather than from fnd.filters to keep this module free of that dependency.
_FRONTMATTER_KINDS: Final[frozenset[str]] = frozenset({"md"})

_TAG_FACTS: Final[dict[str, str]] = {
    "file.tags.os": "os",
    "file.tags.frontmatter": "frontmatter",
}


def is_fact_name(name: str) -> bool:
    """True for any ``file.``-prefixed identifier, known or not."""
    return name.startswith(_PREFIX)


class _Unknown:
    """Sentinel: the fact exists but this platform or file cannot supply it."""

    __slots__ = ()


_UNKNOWN: Final = _Unknown()


class FileFacts(Mapping[str, object]):
    """Reserved ``file.*`` attributes plus the file's frontmatter, computed lazily."""

    __slots__ = (
        "_cache",
        "_fm",
        "_fm_read",
        "_path",
        "_providers",
        "_read_fm",
        "_root",
        "_tag_cache",
    )

    def __init__(
        self,
        path: Path,
        *,
        root: Path,
        read_frontmatter: Callable[[Path], dict[str, object] | None] | None = None,
        tag_providers: Sequence[TagProvider] = (),
    ) -> None:
        self._path = path
        self._root = root
        # Injected by the indexer to bound cloud-fetch waits; a plain read
        # would block on an evicted file for as long as the network takes.
        self._read_fm = read_frontmatter or read_frontmatter_from_file
        self._providers = tuple(tag_providers)
        self._cache: dict[str, object] = {}
        self._fm: dict[str, object] = {}
        self._fm_read = False
        self._tag_cache: dict[str, dict[str, tuple[str, ...]]] = {}

    # ── Mapping ──────────────────────────────────────────────────

    def __getitem__(self, key: str) -> object:
        if key in self._cache:
            value = self._cache[key]
        elif is_fact_name(key):
            value = self._cache.setdefault(key, self._compute_fact(key))
        else:
            fm = self._frontmatter()
            if key not in fm:
                raise KeyError(key)
            return fm[key]
        if value is _UNKNOWN:
            raise KeyError(key)
        return value

    def __iter__(self) -> Iterator[str]:
        """Forces a frontmatter read. The evaluator never iterates; this is
        here to honour the Mapping contract, not because anything hot uses it."""
        yield from RESERVED_FACTS
        yield from self._frontmatter()

    def __len__(self) -> int:
        return len(RESERVED_FACTS) + len(self._frontmatter())

    # ── Unknown-vs-absent ────────────────────────────────────────

    def is_unknown(self, key: str) -> bool:
        """True when a reserved fact exists but could not be determined.

        A frontmatter key is never unknown, only absent — the strict-null rule
        already drops a file whose frontmatter does not answer the question.
        """
        if not is_fact_name(key):
            return False
        if key not in self._cache:
            self._cache[key] = self._compute_fact(key)
        return self._cache[key] is _UNKNOWN

    # ── Computation ──────────────────────────────────────────────

    def _frontmatter(self) -> dict[str, object]:
        if self._fm_read:
            return self._fm
        self._fm_read = True
        if kind_for_suffix(self._path.suffix) not in _FRONTMATTER_KINDS:
            # Reading a PDF or a CSV as text to look for a YAML block opens
            # every candidate in the source for nothing.
            return self._fm
        try:
            self._fm = self._read_fm(self._path) or {}
        except (FrontmatterParseError, OSError, ValueError):
            # A malformed or unreadable file has no frontmatter; it must not
            # take the index run down with it.
            self._fm = {}
        return self._fm

    def _compute_fact(self, key: str) -> object:
        if key not in RESERVED_FACTS:
            return _UNKNOWN
        if key in _TAG_FACTS:
            source = _TAG_FACTS[key]
            return self._tags(only=source).get(source, ())
        match key:
            case "file.path":
                return self._relative()
            case "file.name":
                return self._path.name
            case "file.ext":
                return self._path.suffix.lower()
            case "file.kind":
                return kind_for_suffix(self._path.suffix) or _UNKNOWN
            case "file.category":
                kind = kind_for_suffix(self._path.suffix)
                spec = KIND_BY_ID.get(kind or "")
                return spec.category if spec is not None else _UNKNOWN
            case "file.hidden":
                return any(part.startswith(".") for part in Path(self._relative()).parts)
            case "file.tags.all":
                merged: set[str] = set()
                for values in self._tags().values():
                    merged |= set(values)
                return tuple(sorted(merged))
            case "file.size":
                try:
                    return self._path.stat().st_size
                except OSError:
                    return _UNKNOWN
            case _:
                return self._timestamp(key)

    def _timestamp(self, key: str) -> object:
        times = read_file_times(self._path)
        stamp = times.created if key == "file.created" else times.mtime
        # 0 is fsmeta's "no information" (ext4 without statx birth time), not
        # the epoch — reporting it as a real date would filter on a lie.
        if stamp <= 0:
            return _UNKNOWN
        # Local, not UTC: a bound names the calendar day the user typed. East
        # of UTC a morning edit reads as the previous day in UTC, so a file
        # touched on the bound date would be dropped.
        return dt.datetime.fromtimestamp(stamp).date()

    def _tags(self, *, only: str | None = None) -> dict[str, tuple[str, ...]]:
        """Tags per provider, computing only the provider a fact names.

        ``file.tags.os`` therefore costs one xattr and never opens the file;
        only ``file.tags.frontmatter`` and ``file.tags.all`` read content.
        """
        cache_key = only or "*"
        cached = self._tag_cache.get(cache_key)
        if cached is not None:
            return cached
        providers = [p for p in self._providers if only is None or p.id == only]
        needs_content = any(p.id != "os" for p in providers)
        ctx = TagContext(
            path=self._path,
            frontmatter=(self._frontmatter() or None) if needs_content else None,
        )
        # Sorted tuples, not frozensets: the DSL's membership test rejects any
        # container it cannot order, and tuples keep rendering deterministic.
        by_source = {src: tuple(sorted(vals)) for src, vals in read_tags(ctx, providers).items()}
        self._tag_cache[cache_key] = by_source
        return by_source

    def _relative(self) -> str:
        try:
            return self._path.relative_to(self._root).as_posix()
        except ValueError:
            return self._path.as_posix()

"""Closed value sets for the filters a user can name on the command line.

A :class:`Vocabulary` is the legal universe for one filter — collection
names, file kinds, date tokens — so a value outside it fails loudly with a
suggestion rather than surviving as a clause that can never match. Both
failure modes are silent today: an unknown ``-c`` name becomes a hard
``F_COLLECTION`` term that matches nothing, and an unknown ``--kind``
becomes a ``kind:`` clause against a field that only stores registry ids.

Whether case matters is per-vocabulary, and it tracks what the engine
actually does with the value. Collection names reach a ``raw``-tokenised
field verbatim, so ``dpc2`` really is a different value from ``DPC2`` and the
user gets asked; file kinds are lowercased on the way into the query, so
``PDF`` is just a spelling of ``pdf`` and resolves without comment.

Suggestions reuse :mod:`fnd.matching`'s capped OSA distance and its
Lucene-AUTO length→tolerance policy, so the tolerance for a typo'd filter
value is the same one the search cascade already applies to query terms.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fnd.matching import auto_fuzzy_distance, osa_within
from fnd.query_errors import UnknownFilterValueError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from fnd.config import Config

# More near-misses than this and "did you mean" stops being a suggestion.
_MAX_SUGGESTIONS = 3

__all__ = [
    "Vocabulary",
    "collection_vocabulary",
    "date_vocabulary",
    "kind_vocabulary",
    "tag_match_vocabulary",
]


class Vocabulary:
    """The legal values for one filter, with typo-tolerant lookup.

    ``label`` is the noun used in error text ("collection" → *no collection
    named 'dpc2'*), so it reads naturally for every filter that has one of
    these.

    ``case_sensitive`` says whether case survives into the query. Set it when
    it does: a case variant then stops being a match and becomes the leading
    suggestion, which is the difference between silently searching the wrong
    thing and being asked "did you mean 'DPC2'?".
    """

    def __init__(self, label: str, names: Iterable[str], *, case_sensitive: bool = False) -> None:
        self.label = label
        self.case_sensitive = case_sensitive
        self.names: tuple[str, ...] = tuple(dict.fromkeys(names))
        self._exact = frozenset(self.names)
        # casefold → canonical spelling. First spelling wins if a config
        # somehow holds two names differing only by case.
        self._by_fold: dict[str, str] = {}
        for name in self.names:
            self._by_fold.setdefault(name.casefold(), name)

    def match(self, raw: str) -> str | None:
        """Canonical spelling of ``raw``, or None if it isn't a legal value."""
        value = raw.strip()
        if value in self._exact:
            return value
        if self.case_sensitive:
            return None
        return self._by_fold.get(value.casefold())

    def suggest(self, raw: str) -> list[str]:
        """Names close enough to ``raw`` to have been the intent, best first.

        A case-only variant always qualifies — it costs no edits, and on a
        case-sensitive vocabulary it's the whole reason the value missed.
        Anything else has to fall inside the usual typo tolerance.

        Only the closest tier is returned. With ``DPC`` and ``DPC2`` both
        configured, ``dpc2`` is one exactly and the other with an edit; a
        weaker candidate shouldn't turn an obvious fix into a choice. Ties
        within the tier are genuine ambiguity and are all returned (capped).
        """
        value = raw.strip().casefold()
        if not value:
            return []
        budget = auto_fuzzy_distance(value)
        scored: list[tuple[int, str]] = []
        for name in self.names:
            folded = name.casefold()
            if folded == value:
                scored.append((0, name))
            elif budget and (d := osa_within(value, folded, max_dist=budget)) <= budget:
                scored.append((d, name))
        if not scored:
            return []
        scored.sort()
        best = scored[0][0]
        return [name for distance, name in scored[:_MAX_SUGGESTIONS] if distance == best]

    def resolve(self, raw: str, *, flag: str | None = None) -> str:
        """Canonical spelling of ``raw``, or raise with the near-misses."""
        hit = self.match(raw)
        if hit is not None:
            return hit
        raise self.unknown(raw, flag=flag)

    def split_resolve(self, raw: str) -> tuple[list[str], list[str]]:
        """Resolve a comma-separated value into (canonical known, unknown).

        A whole-string match is tried first so a legacy config name that
        itself contains a comma still resolves; only then is the value split.
        Names may legally contain spaces, so parts are stripped, not
        tokenised. Each caller decides what to do with the unknowns — the CLI
        reports them, the sidebar drops them.
        """
        whole = self.match(raw)
        if whole is not None:
            return [whole], []
        known: list[str] = []
        unknown: list[str] = []
        for part in (p.strip() for p in raw.split(",")):
            if not part:
                continue
            hit = self.match(part)
            if hit is None:
                unknown.append(part)
            else:
                known.append(hit)
        return list(dict.fromkeys(known)), list(dict.fromkeys(unknown))

    def unknown(self, raw: str, *, flag: str | None = None) -> UnknownFilterValueError:
        """Build the error for ``raw`` without raising it — for callers that
        collect several problems before reporting any of them."""
        return UnknownFilterValueError(
            label=self.label,
            value=raw,
            suggestions=self.suggest(raw),
            known=self.names,
            flag=flag,
        )


def collection_vocabulary(config: Config) -> Vocabulary:
    """Collections defined in the user's config TOML.

    Case-sensitive: ``F_COLLECTION`` uses the ``raw`` tokenizer and stores the
    config key verbatim, so ``dpc2`` would match nothing at all.

    The ``all`` pseudo-name is deliberately absent: callers check
    :func:`fnd.config.is_all_collections` first, which lets a real
    collection literally named ``all`` keep winning over the pseudo-name.
    """
    return Vocabulary("collection", config.collections, case_sensitive=True)


def kind_vocabulary() -> Vocabulary:
    """Fine-grained file kinds plus the category ids that expand into them.

    Case-insensitive: kind clauses are lowercased when compiled, so ``PDF``
    already works and shouldn't be interrogated.
    """
    from fnd.kinds import ALL_KIND_IDS, CATEGORY_BY_ID

    return Vocabulary("file kind", (*ALL_KIND_IDS, *CATEGORY_BY_ID))


def date_vocabulary() -> Vocabulary:
    """Relative-date keywords accepted by ``--created`` / ``--modified``."""
    from fnd.query_dsl import _DATE_TOKEN_DAYS

    return Vocabulary("date token", _DATE_TOKEN_DAYS)


def tag_match_vocabulary() -> Vocabulary:
    """How repeated ``--tag`` flags combine."""
    return Vocabulary("tag-match mode", ("all", "any"))

"""Term resolvers: turn a single query term into the set of indexed F_BODY
stems it should match, then a BM25-scored ``term_query`` OR over them.

``F_BODY`` is analyzed with ``en_stem`` (Snowball English), so the on-disk token
for "Templates" is ``templat``. Resolvers that bypass ``parse_query`` (the fuzzy
pass, the wildcard path) must consult the stemmed term dictionary directly and
emit plain ``term_query`` clauses — so matched docs land on real BM25 scoring,
not Tantivy's constant-1.0 ``fuzzy_term_query`` / ``RegexQuery`` output. This is
the same MultiTermQuery rewrite Lucene applies.

Lifted out of ``fnd.cascade`` so the cascade fuzzy pass, the single-pass fuzzy
path, and the wildcard path share one implementation.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import snowballstemmer
import tantivy

from fnd.matching import osa_within
from fnd.schema import F_BODY

if TYPE_CHECKING:
    from fnd.query import Searcher

# threading.local: snowballstemmer instances aren't thread-safe.
_STEMMER_LOCAL = threading.local()

# Cap on dictionary entries scanned per character bucket. A typical English
# corpus has ~20-50k unique stems per leading character; the cap bounds the
# worst-case scan on huge corpora without losing matches in normal ones.
_DICT_LIMIT = 50_000


def fuzzy_stem(term: str) -> str:
    """Snowball-stem a query term to the on-disk F_BODY token form."""
    s = getattr(_STEMMER_LOCAL, "instance", None)
    if s is None:
        s = snowballstemmer.stemmer("english")
        _STEMMER_LOCAL.instance = s
    return s.stemWord(term.lower())


def fuzzy_variants(searcher: Searcher, stem: str, max_dist: int) -> list[str]:
    """Enumerate indexed F_BODY stems within ``max_dist`` edits of ``stem``.

    Anchors the term-dictionary scan to the stem's first character (Lucene's
    MultiTermQuery trick). ``max_dist == 0`` returns ``[stem]`` iff indexed.
    The exact stem is returned first so it becomes the first OR sub-clause.
    """
    if max_dist == 0:
        return [
            t for t, _ in searcher._searcher.terms_with_prefix(F_BODY, stem, limit=1) if t == stem
        ]
    if not stem:
        return []
    candidates = searcher._searcher.terms_with_prefix(F_BODY, stem[0], limit=_DICT_LIMIT)
    out: list[str] = []
    seen = False
    for term, _count in candidates:
        if term == stem:
            seen = True
            continue
        if osa_within(term, stem, max_dist=max_dist) <= max_dist:
            out.append(term)
    if seen:
        out.insert(0, stem)
    return out


def prefix_variants(searcher: Searcher, prefix: str, *, limit: int = _DICT_LIMIT) -> list[str]:
    """Enumerate indexed F_BODY stems that start with ``prefix`` (a trailing
    ``term*`` wildcard).

    The prefix is lowercased but NOT stemmed: it is matched literally against
    the stemmed dictionary, so ``crypto*`` finds the stems ``crypto`` /
    ``cryptographi`` / ``cryptograph`` (and not ``cryptid``). This is what makes
    the wildcard work where ``parse_query`` — which silently drops ``*`` — does
    not.
    """
    pre = prefix.lower()
    if not pre:
        return []
    return [t for t, _ in searcher._searcher.terms_with_prefix(F_BODY, pre, limit=limit)]


def term_or_query(schema: tantivy.Schema, terms: list[str]) -> tantivy.Query | None:
    """BM25-scored OR of ``term_query`` over ``terms`` on F_BODY. None if empty,
    the bare term_query for one, a Should-boolean for several."""
    queries = [tantivy.Query.term_query(schema, F_BODY, t) for t in terms]
    if not queries:
        return None
    if len(queries) == 1:
        return queries[0]
    return tantivy.Query.boolean_query([(tantivy.Occur.Should, q) for q in queries])

"""Term resolvers: turn a single query term into the indexed terms it should
match, then a BM25-scored ``term_query`` OR over them.

Fuzzy terms resolve against F_BODY's stems (``fnd_text`` stores "Templates" as
``templat``); wildcards and regexes against F_WORDS, the same words unstemmed,
so a pattern reads the word as written. Both emit plain ``term_query`` clauses,
so matched docs land on BM25, not tantivy's constant-1.0 ``fuzzy_term_query`` /
``RegexQuery`` output: the MultiTermQuery rewrite Lucene applies.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING

import tantivy

from fnd import regex_terms
from fnd.analysis import index_token
from fnd.matching import glob_to_regex, leading_edits, osa_within
from fnd.schema import F_BODY, F_WORDS

if TYPE_CHECKING:
    from fnd.query import Searcher

# Cap on dictionary entries scanned per character bucket. A typical English
# corpus has ~20-50k unique stems per leading character; the cap bounds the
# worst-case scan on huge corpora without losing matches in normal ones.
_DICT_LIMIT = 50_000


def fuzzy_variants(
    searcher: Searcher, stem: str, max_dist: int, *, front: bool = False
) -> list[str]:
    """Indexed F_BODY stems within ``max_dist`` edits of ``stem``, the exact stem
    first, as :func:`fnd.matching.fuzzy_reaches` admits them (``front``: one edit
    may change the first letter)."""
    index = searcher._searcher
    if max_dist == 0:
        return [t for t, _ in index.terms_with_prefix(F_BODY, stem, limit=1) if t == stem]
    if not stem:
        return []
    out: list[str] = []
    seen = False
    for term, _count in index.terms_with_prefix(F_BODY, stem[0], limit=_DICT_LIMIT):
        if term == stem:
            seen = True
        elif osa_within(term, stem, max_dist=max_dist) <= max_dist:
            out.append(term)
    if front:
        out.extend(t for t in leading_edits(stem) if index.doc_freq(F_BODY, t) > 0)
    if seen:
        out.insert(0, stem)
    return out


def prefix_variants(searcher: Searcher, prefix: str, *, limit: int = _DICT_LIMIT) -> list[str]:
    """Indexed F_WORDS words that start with ``prefix`` (a trailing ``term*``)."""
    pre = prefix.lower()
    if not pre:
        return []
    return [t for t, _ in searcher._searcher.terms_with_prefix(F_WORDS, pre, limit=limit)]


_REGEX_META = frozenset("\\.^$*+?()[]{}|")
_QUANTIFIERS = frozenset("*?{")
# Recall comes from the pattern itself; these many most frequent terms rank it.
PATTERN_TERMS = 256


def pattern_variants(searcher: Searcher, pattern: str, *, glob: bool) -> dict[str, int] | None:
    """Indexed F_WORDS words the regex ``pattern`` matches whole, with document
    frequencies, most frequent first; None for a regex with no literal prefix.
    Regexes run on tantivy's engine as the highlighter's do (fnd.regex_terms)."""
    prefixes = _literal_prefixes(pattern)
    if prefixes is None and not glob:
        return None
    index = searcher._searcher
    if prefixes is None:
        counts = _whole_dictionary(searcher)
    else:
        counts = {}
        for prefix in prefixes:
            counts.update(index.terms_with_prefix(F_WORDS, prefix, limit=_DICT_LIMIT))
    if glob:
        compiled = re.compile(pattern, re.IGNORECASE)
        matched = {t for t in counts if compiled.fullmatch(t)}
    else:
        matched = regex_terms.matching(pattern, counts)
    return {t: counts[t] for t in sorted(matched, key=lambda t: -counts[t])}


# A phrase's wildcard word matching more words than this runs as a glob over stems,
# keeping the positional regex small.
PHRASE_MEMBER_WORDS = 512


def phrase_member_stems(searcher: Searcher, glob: str) -> frozenset[str] | None:
    """The F_BODY stems a phrase's wildcard word searches (those of the words it
    matches), or None where it runs as a glob over the stems themselves."""
    variants = pattern_variants(searcher, glob_to_regex(glob), glob=True) or {}
    if len(variants) > PHRASE_MEMBER_WORDS:
        return None
    return frozenset(index_token(w) for w in variants)


def blended_term_query(
    schema: tantivy.Schema, terms: list[str], doc_freq: dict[str, int], docs: int
) -> tantivy.Query | None:
    """An OR over ``terms``, each boosted by IDF(most common) / IDF(its own), as
    Lucene's blended rewrite: left to BM25, ``cr*to`` ranked "cristo" above "crypto"."""
    if not terms:
        return None
    common = _idf(max(doc_freq[t] for t in terms), docs)
    clauses = [
        (
            tantivy.Occur.Should,
            tantivy.Query.boost_query(
                tantivy.Query.term_query(schema, F_WORDS, t), common / _idf(doc_freq[t], docs)
            ),
        )
        for t in terms
    ]
    return clauses[0][1] if len(clauses) == 1 else tantivy.Query.boolean_query(clauses)


def _idf(df: int, docs: int) -> float:
    return math.log(1 + (docs - df + 0.5) / (df + 0.5))


# The word dictionary of the last index commit scanned whole (a few hundred thousand
# terms, about 250 ms to read). Keyed by commit, as the TUI reloads before every query.
_dictionary: tuple[object, dict[str, int]] | None = None


def _whole_dictionary(searcher: Searcher) -> dict[str, int]:
    global _dictionary
    if _dictionary is None or _dictionary[0] != searcher.generation:
        terms = searcher._searcher.terms_with_prefix(F_WORDS, "", limit=100_000_000)
        _dictionary = (searcher.generation, dict(terms))
    return _dictionary[1]


def _literal_prefixes(pattern: str) -> list[str] | None:
    """The literal text each top-level alternative starts with, lowercased, or
    None when an alternative starts with a metacharacter."""
    alternatives: list[str] = []
    depth, start = 0, 0
    for i, ch in enumerate(pattern):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "|" and depth == 0:
            alternatives.append(pattern[start:i])
            start = i + 1
    alternatives.append(pattern[start:])
    out = []
    for alt in alternatives:
        end = 0
        while end < len(alt) and alt[end] not in _REGEX_META:
            end += 1
        if end < len(alt) and alt[end] in _QUANTIFIERS:
            end -= 1  # the quantifier makes the last literal optional
        if end <= 0:
            return None
        out.append(alt[:end].lower())
    return out


def term_or_query(
    schema: tantivy.Schema, terms: list[str], field: str = F_BODY
) -> tantivy.Query | None:
    """BM25-scored OR of ``term_query`` over ``terms`` on ``field``. None if empty,
    the bare term_query for one, a Should-boolean for several."""
    queries = [tantivy.Query.term_query(schema, field, t) for t in terms]
    if not queries:
        return None
    if len(queries) == 1:
        return queries[0]
    return tantivy.Query.boolean_query([(tantivy.Occur.Should, q) for q in queries])

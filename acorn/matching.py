"""Shared match semantics for the search and highlight pipelines.

The cascade fuzzy pass (`acorn.cascade._fuzzy_pass`) widens a query
into "any indexed stem within edit distance N of a query stem" — same
shape as Lucene's ``MultiTermQuery`` rewrite. The preview highlighter
needs to mark every word that *would* have caused a hit so the user
sees why a chunk surfaced (literal exact, fuzzy variant, or synonym
expansion). Both pipelines share the helpers in this module so their
notion of "matches" can never drift.

Public surface:

* :class:`MatchSpec` — frozen carrier for the per-query match data.
* :func:`MatchSpec.from_query` — factory that pre-stems the query
  terms, applies optional synonym expansion, and per-stem assigns the
  Lucene-AUTO fuzzy distance.
* :func:`word_matches` — predicate evaluated on each rendered word.
* :func:`auto_fuzzy_distance` — exposed for cascade so it picks the
  same per-term distance bound the highlighter uses.
* :func:`levenshtein_within` — capped edit distance with early exit.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import snowballstemmer

from acorn.synonyms import SynonymTable, expand

# snowballstemmer holds per-call cursor state; not thread-safe.
_STEMMER_LOCAL = threading.local()


def _stem(word: str) -> str:
    stemmer = getattr(_STEMMER_LOCAL, "instance", None)
    if stemmer is None:
        stemmer = snowballstemmer.stemmer("english")
        _STEMMER_LOCAL.instance = stemmer
    return stemmer.stemWord(word.lower())


def auto_fuzzy_distance(stem: str) -> int:
    """Lucene's ``fuzziness=AUTO`` heuristic: longer terms tolerate
    more typos. ``≤2`` chars → exact only (typos in 1-2 char tokens
    almost always change meaning); 3-5 → distance 1; ≥6 → distance 2.
    Operates on the post-stem token because that's what the index
    stores and what the cascade fuzzy pass enumerates against.
    """
    n = len(stem)
    if n <= 2:
        return 0
    if n <= 5:
        return 1
    return 2


def levenshtein_within(a: str, b: str, *, max_dist: int) -> int:
    """Capped Levenshtein edit distance. Returns ``max_dist + 1`` when
    the true distance exceeds ``max_dist`` (early-exit when the
    running minimum row already beats the cap). Avoids importing
    ``rapidfuzz``/``Levenshtein`` so the matching layer stays
    dependency-free.
    """
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if abs(la - lb) > max_dist:
        return max_dist + 1
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev = list(range(lb + 1))
    for i, ca in enumerate(a, 1):
        curr = [i] + [0] * lb
        row_min = i
        for j, cb in enumerate(b, 1):
            ins = curr[j - 1] + 1
            dele = prev[j] + 1
            sub = prev[j - 1] + (0 if ca == cb else 1)
            v = ins if ins < dele else dele
            if sub < v:
                v = sub
            curr[j] = v
            if v < row_min:
                row_min = v
        if row_min > max_dist:
            return max_dist + 1
        prev = curr
    return prev[lb]


@dataclass(frozen=True, slots=True)
class MatchSpec:
    """Frozen description of which words count as "matches" for a query.

    ``exact_stems`` is the union of:

    * The stems of every plain-word query term (``_terms_from_query``
      output, lowercased and Snowball-stemmed).
    * Stems of every synonym variant the SynonymTable would expand
      those terms into — so a query for "k8s" highlights "kubernetes"
      in the doc.

    ``fuzzy_per_stem`` lists ``(stem, max_distance)`` pairs for every
    query stem whose AUTO distance is non-zero, used to highlight
    fuzzy variants the cascade pass would have surfaced. Empty when
    fuzzy matching is disabled (e.g. tests that want strict-stem
    semantics).

    ``raw_terms`` carries the lowercased typed words (plus synonym
    variants) so the highlighter can align a fuzzy-matched doc word
    against the actual typed form, not the post-stem token. The
    char-level colour overlay (yellow for matching chars, orange for
    mismatches) reads from this field.
    """

    exact_stems: frozenset[str] = field(default_factory=frozenset)
    fuzzy_per_stem: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    raw_terms: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_query(
        cls,
        query: str,
        *,
        synonyms: SynonymTable | None = None,
        fuzzy: bool = True,
    ) -> MatchSpec:
        """Build a spec from a user query string.

        Imports :func:`acorn.render._terms_from_query` lazily to avoid
        a render-side dependency on the parser (render already
        depends on this module via the highlight helpers).
        """
        from acorn.render import _terms_from_query  # local import: avoid cycle

        terms = _terms_from_query(query)
        if not terms:
            return cls()
        raw = {t.lower() for t in terms if t}
        exact = {_stem(t) for t in raw}
        # Pull synonym variants in: the cascade's synonym pass would
        # have surfaced docs containing them, so the highlighter
        # marks them too.
        if synonyms is not None and synonyms.groups:
            expanded = expand(query, synonyms)
            if expanded != query:
                expanded_terms = _terms_from_query(expanded)
                for t in expanded_terms:
                    if t:
                        raw.add(t.lower())
                        exact.add(_stem(t))
        if not fuzzy:
            return cls(
                exact_stems=frozenset(exact),
                fuzzy_per_stem=(),
                raw_terms=tuple(sorted(raw)),
            )
        # Fuzzy variants: per-stem AUTO distance. Stems with distance 0
        # don't add anything beyond exact-match, so omit them.
        fuzzy_pairs = tuple(
            (s, auto_fuzzy_distance(s)) for s in exact if auto_fuzzy_distance(s) > 0
        )
        return cls(
            exact_stems=frozenset(exact),
            fuzzy_per_stem=fuzzy_pairs,
            raw_terms=tuple(sorted(raw)),
        )

    @property
    def is_empty(self) -> bool:
        return not self.exact_stems and not self.fuzzy_per_stem


def word_matches(word: str, spec: MatchSpec) -> bool:
    """True if ``word`` matches ``spec`` under any of the cascade's
    pass semantics: exact-stem (literal / phrase / synonym) or
    fuzzy-AUTO."""
    if spec.is_empty or not word:
        return False
    s = _stem(word)
    if s in spec.exact_stems:
        return True
    for q_stem, max_d in spec.fuzzy_per_stem:
        if levenshtein_within(s, q_stem, max_dist=max_d) <= max_d:
            return True
    return False


def align_doc_word(doc_word: str, query_word: str) -> list[bool]:
    """Char-level alignment of ``doc_word`` against ``query_word`` via
    Levenshtein traceback.

    Returns one ``bool`` per char of ``doc_word``: ``True`` when that
    char aligns to an identical char in ``query_word`` (a "match"
    character), ``False`` otherwise (substitution or insertion — the
    doc has a char the query didn't have, or a different char in its
    place). Deletions on the query side don't produce a doc char and
    are silently absorbed.

    Comparison is case-insensitive — case differences alone don't
    count as mismatches.
    """
    a = query_word.lower()
    b = doc_word.lower()
    m, n = len(a), len(b)
    if n == 0:
        return []
    if m == 0:
        return [False] * n  # everything is "extra" relative to the empty query
    # Standard edit-distance DP table. dp[i][j] = distance between
    # a[:i] and b[:j].
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m + 1):
        dp[i][0] = i
    for j in range(n + 1):
        dp[0][j] = j
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                dp[i][j] = 1 + min(dp[i - 1][j - 1], dp[i - 1][j], dp[i][j - 1])
    # Traceback. Per-doc-char (b-side) result; default to False so any
    # doc char we don't visit (shouldn't happen but defensive) reads
    # as a mismatch rather than silently disappearing.
    matches: list[bool] = [False] * n
    i, j = m, n
    while i > 0 or j > 0:
        if i > 0 and j > 0 and a[i - 1] == b[j - 1]:
            matches[j - 1] = True
            i -= 1
            j -= 1
        elif i > 0 and j > 0 and dp[i][j] == dp[i - 1][j - 1] + 1:
            # Substitution — doc has a different char at this position.
            matches[j - 1] = False
            i -= 1
            j -= 1
        elif j > 0 and dp[i][j] == dp[i][j - 1] + 1:
            # Insertion — doc has an extra char the query didn't.
            matches[j - 1] = False
            j -= 1
        elif i > 0:
            # Deletion — query had a char missing from doc; nothing to
            # paint on the doc side.
            i -= 1
        else:
            break
    return matches


def closest_raw_term(doc_word: str, spec: MatchSpec) -> str | None:
    """Return the typed query term (lowercased) with the smallest
    case-insensitive edit distance to ``doc_word``. Used by the
    highlighter to align fuzzy-matched doc words against the actual
    word the user typed (not its post-stem form).

    Returns ``None`` when ``spec`` has no raw terms (e.g. an empty
    query or a spec built before the field was populated).
    """
    if not spec.raw_terms:
        return None
    target = doc_word.lower()
    best_term: str | None = None
    best_dist = -1
    for term in spec.raw_terms:
        cap = max(len(target), len(term))
        d = levenshtein_within(target, term, max_dist=cap)
        if best_term is None or d < best_dist:
            best_term = term
            best_dist = d
    return best_term

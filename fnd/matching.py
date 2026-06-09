"""Shared match semantics for the search and highlight pipelines.

The cascade fuzzy pass (`fnd.cascade._fuzzy_pass`) widens a query
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

import re
import threading
from dataclasses import dataclass, field
from itertools import pairwise

import snowballstemmer

from fnd.stopwords import STOPWORDS
from fnd.synonyms import SynonymTable, expand

# A double-quoted run is a phrase (Tantivy phrase syntax). Single quotes are
# left to ordinary term extraction (they carry collection names, apostrophes).
_QUOTED_PHRASE = re.compile(r'"([^"]*)"')

# Content-token patterns mirroring fnd.query so highlighting expands a match the
# same way the search did: trailing wildcard (prefix), /regex/, infix/leading
# glob. All matched against the *stemmed* doc word, matching search semantics.
_HL_REGEX = re.compile(r"^/(.+)/$")
_HL_GLOB = re.compile(r"[*?]")

# Boolean operators are query structure, not content — they must never become
# highlight terms or consume a colour slot.
_BOOL_KEYWORDS = frozenset({"AND", "OR", "NOT"})


def glob_to_regex(glob: str) -> str:
    """Translate a shell glob (``*`` → any run, ``?`` → one char) into a regex
    matching a whole term; other chars are escaped and the glob lowercased.
    Shared by the search resolvers and the highlighter so both expand a wildcard
    identically."""
    return "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in glob.lower())


def _glob_capture_regex(glob: str) -> str:
    """Like :func:`glob_to_regex`, but wraps each ``*``/``?`` fill in a capturing
    group so the highlighter can colour wildcard-filled chars distinctly from the
    literal (typed) chars."""
    return "".join(
        "(.*)" if c == "*" else "(.)" if c == "?" else re.escape(c) for c in glob.lower()
    )


def _phrase_word_lists(query: str) -> list[list[str]]:
    """Raw word lists for each quoted phrase of two or more words."""
    out: list[list[str]] = []
    for m in _QUOTED_PHRASE.finditer(query):
        words = re.findall(r"\w+", m.group(1))
        if len(words) >= 2:
            out.append(words)
    return out


def _strip_quoted_spans(query: str) -> str:
    """Query with quoted-phrase contents removed — leaves only loose terms."""
    return _QUOTED_PHRASE.sub(" ", query)


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


def osa_within(a: str, b: str, *, max_dist: int) -> int:
    """Capped optimal-string-alignment (restricted Damerau-Levenshtein)
    distance: an adjacent transposition costs 1, not 2. Matches Tantivy /
    Lucene fuzzy semantics (``transposition_cost_one``), so a typo like
    ``mitochondira`` → ``mitochondria`` resolves at distance 1. Returns
    ``max_dist + 1`` once the running row minimum exceeds the cap.
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
    prev2: list[int] | None = None
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
            # Adjacent transposition: a[i-1]a[i] == b[j]b[j-1].
            if prev2 is not None and i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                t = prev2[j - 2] + 1
                if t < v:
                    v = t
            curr[j] = v
            if v < row_min:
                row_min = v
        if row_min > max_dist:
            return max_dist + 1
        prev2 = prev
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
    query stem whose resolved distance is non-zero, used to highlight
    fuzzy variants the cascade pass would have surfaced. Empty when
    auto-fuzzy is disabled *and* the query has no explicit ``~N``
    opt-ins. Sorted by stem so the output is deterministic.

    ``raw_terms`` carries the lowercased typed words (plus synonym
    variants) so the highlighter can align a fuzzy-matched doc word
    against the actual typed form, not the post-stem token. The
    char-level colour overlay (yellow for matching chars, orange for
    mismatches) reads from this field.
    """

    exact_stems: frozenset[str] = field(default_factory=frozenset)
    fuzzy_per_stem: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    raw_terms: tuple[str, ...] = field(default_factory=tuple)
    # Original glob tokens (``discoun*``, ``cr*to``, ``*graph``, ``gr?y``) and
    # literal ``/regex/`` patterns, matched against the stemmed doc word — so
    # wildcard, regex, and glob matches highlight the same words the search
    # surfaced. The glob tokens are kept verbatim (not pre-compiled) so the
    # highlighter can colour literal chars yellow and wildcard-filled chars
    # orange. Kept out of ``raw_terms`` (the fuzzy char-aligner).
    wildcards: tuple[str, ...] = field(default_factory=tuple)
    regexes: tuple[str, ...] = field(default_factory=tuple)
    # Ordered, de-duplicated ``(kind, key, dist)`` per distinct typed term, in
    # query order, so each term highlights in its own colour. kind is
    # ``term`` (key=stem, dist>0 ⇒ fuzzy) / ``wildcard`` (key=glob) /
    # ``regex`` (key=pattern). Drives :func:`match_color`.
    order: tuple[tuple[str, str, int], ...] = field(default_factory=tuple)
    # Stem sequences for each quoted phrase. Highlighted as a contiguous
    # span (see :func:`phrase_char_spans`); their words are deliberately
    # kept OUT of ``exact_stems`` so a stopword inside a phrase ("in",
    # "and") doesn't light up document-wide.
    phrases: tuple[tuple[str, ...], ...] = field(default_factory=tuple)

    @classmethod
    def from_query(
        cls,
        query: str,
        *,
        synonyms: SynonymTable | None = None,
        auto_fuzzy: bool = True,
        min_term_chars: int = 0,
        multicolour: bool = True,
    ) -> MatchSpec:
        """Build a spec from a user query string.

        ``auto_fuzzy`` controls the AUTO heuristic the same way the
        cascade pass does. ``min_term_chars`` is the post-stem floor
        below which auto-fuzzy is suppressed. Per-term ``~N`` modifiers
        in the query always produce fuzzy pairs, regardless of either
        knob — explicit user opt-in overrides the safety net.

        Imports :func:`fnd.render._terms_from_query` lazily to avoid
        a render-side dependency on the parser (render already
        depends on this module via the highlight helpers).
        """
        from fnd.cascade import _terms_with_fuzzy  # local import: avoid cycle
        from fnd.render import _terms_from_query  # local import: avoid cycle

        # Quoted phrases are matched as contiguous spans; their words are
        # kept out of the loose (document-wide) term set, so only the
        # unquoted remainder drives word-by-word highlighting.
        quoted_word_lists = _phrase_word_lists(query)
        phrases = tuple(tuple(_stem(w) for w in words) for words in quoted_word_lists)
        loose_query = _strip_quoted_spans(query)

        # Wildcard / regex tokens: highlight any doc word whose stem matches the
        # same pattern the search expanded. Stored verbatim (globs) so the
        # highlighter can colour literal vs wildcard-filled chars. They are then
        # dropped from the plain-term run below so the bare residual (``crypto``
        # from ``crypto*``) doesn't also auto-fuzzy-highlight.
        wildcards: list[str] = []
        regexes: list[str] = []
        plain_tokens: list[str] = []
        ordered_tokens: list[tuple[str, str]] = []  # (kind, key) in query order
        for tok in loose_query.split():
            if tok in _BOOL_KEYWORDS:  # AND/OR/NOT: structure, not a highlight term
                continue
            rm = _HL_REGEX.match(tok)
            if rm:
                regexes.append(rm.group(1).lower())
                ordered_tokens.append(("regex", rm.group(1).lower()))
            elif _HL_GLOB.search(tok):
                wildcards.append(tok.lower())
                ordered_tokens.append(("wildcard", tok.lower()))
            else:
                plain_tokens.append(tok)
                ordered_tokens.append(("plain", tok))
        loose_query = " ".join(plain_tokens)

        # In-context stopwords: a connector like "in" in `defence in depth`
        # should highlight where it sits next to a matched content word, but a
        # standalone "in" elsewhere should not. Add every consecutive query
        # bigram that pairs a stopword with a content word as an implicit
        # highlight phrase ("defence in", "in depth"); overlapping bigrams also
        # cover the full run. Stopword-only pairs are skipped so bare function
        # words never light up.
        loose_words = _terms_from_query(loose_query, keep_stopwords=True)
        pair_phrases: list[tuple[str, ...]] = []
        for a, b in pairwise(loose_words):
            a_stop, b_stop = a.lower() in STOPWORDS, b.lower() in STOPWORDS
            if (a_stop or b_stop) and not (a_stop and b_stop):
                pair_phrases.append((_stem(a), _stem(b)))
        phrases = phrases + tuple(pair_phrases)

        terms = _terms_from_query(loose_query)
        if not terms and not phrases and not wildcards and not regexes:
            return cls()
        raw = {t.lower() for t in terms if t}
        exact = {_stem(t) for t in raw}
        # Pull synonym variants in: the cascade's synonym pass would
        # have surfaced docs containing them, so the highlighter
        # marks them too.
        if synonyms is not None and synonyms.groups:
            expanded = expand(loose_query, synonyms)
            if expanded != loose_query:
                expanded_terms = _terms_from_query(expanded)
                for t in expanded_terms:
                    if t:
                        raw.add(t.lower())
                        exact.add(_stem(t))
        # Explicit per-term ~N — always honoured (user opt-in).
        explicit_pairs: dict[str, int] = {}
        for term, dist in _terms_with_fuzzy(loose_query):
            if dist is None or dist <= 0:
                continue
            explicit_pairs[_stem(term.lower())] = max(
                explicit_pairs.get(_stem(term.lower()), 0), dist
            )
        auto_pairs: dict[str, int] = {}
        if auto_fuzzy:
            for s in exact:
                if len(s) < min_term_chars:
                    continue
                # Cap AUTO-fuzzy *highlighting* at distance 1. At distance 2 a
                # 6-char term lights up unrelated false friends (defence→defeat,
                # diverse→reverse) — pure noise on a clean query. The search-side
                # cascade and an explicit ``~2`` still use distance 2.
                d = min(auto_fuzzy_distance(s), 1)
                if d > 0:
                    auto_pairs[s] = d
        # Explicit wins on collision (user is asserting a distance).
        merged = {**auto_pairs, **explicit_pairs}
        fuzzy_pairs = tuple(sorted(merged.items()))
        # Colour order: one slot per distinct term, in query order. Repeats and
        # stopwords are skipped (a repeated term keeps its first colour). Left
        # empty when multi-colour highlighting is off, so every term paints in
        # the single slot-0 colour (see :func:`match_color`).
        order: list[tuple[str, str, int]] = []
        seen: set[tuple[str, str]] = set()
        # A quoted phrase renders in the single phrase colour (slot 0). Reserve
        # that slot so the loose terms around it get distinct *following* colours
        # — e.g. `"defence in depth" OR diverse` → phrase yellow, diverse cyan.
        # The sentinel never matches a word in ``match_color`` (it ignores the
        # "phrase" kind); it only occupies the index.
        if multicolour and quoted_word_lists:
            order.append(("phrase", "", 0))
        for kind, key in ordered_tokens if multicolour else []:
            if kind == "plain":
                for w in re.findall(r"\w+", key):
                    if w.lower() in STOPWORDS:
                        continue
                    st = _stem(w)
                    if ("term", st) in seen:
                        continue
                    seen.add(("term", st))
                    order.append(("term", st, merged.get(st, 0)))
            elif (kind, key) not in seen:
                seen.add((kind, key))
                order.append((kind, key, 0))
        return cls(
            exact_stems=frozenset(exact),
            fuzzy_per_stem=fuzzy_pairs,
            raw_terms=tuple(sorted(raw)),
            phrases=phrases,
            wildcards=tuple(wildcards),
            regexes=tuple(regexes),
            order=tuple(order),
        )

    @property
    def is_empty(self) -> bool:
        return not (
            self.exact_stems
            or self.fuzzy_per_stem
            or self.phrases
            or self.wildcards
            or self.regexes
        )


def word_matches(word: str, spec: MatchSpec) -> bool:
    """True if ``word`` matches ``spec`` under any of the search's pass
    semantics: exact-stem (literal / phrase / synonym), wildcard / glob,
    ``/regex/``, or fuzzy. All tested against the word's stem so highlighting
    marks exactly the words the search surfaced."""
    if spec.is_empty or not word:
        return False
    s = _stem(word)
    if s in spec.exact_stems:
        return True
    for pattern in (glob_to_regex(g) for g in spec.wildcards):
        if re.fullmatch(pattern, s) is not None:
            return True
    for pattern in spec.regexes:
        try:
            if re.fullmatch(pattern, s) is not None:
                return True
        except re.error:
            continue
    for q_stem, max_d in spec.fuzzy_per_stem:
        if osa_within(s, q_stem, max_dist=max_d) <= max_d:
            return True
    return False


def match_color(word: str, spec: MatchSpec) -> int:
    """Colour slot of the first ordered query term ``word`` matches — so each
    distinct term in a multi-word query highlights in its own colour. Returns 0
    (the default yellow slot) when no ordered term matches (e.g. a synonym
    variant, or an empty ``order``)."""
    s = _stem(word)
    for i, (kind, key, dist) in enumerate(spec.order):
        if kind == "term":
            if s == key or (dist and osa_within(s, key, max_dist=dist) <= dist):
                return i
        elif kind == "wildcard":
            if re.fullmatch(glob_to_regex(key), s) is not None:
                return i
        elif kind == "regex":
            try:
                if re.fullmatch(key, s) is not None:
                    return i
            except re.error:
                continue
    return 0


def phrase_char_spans(text: str, spec: MatchSpec) -> list[tuple[int, int]]:
    """Character spans of every quoted-phrase occurrence in ``text``.

    Stem-aware and contiguous: a phrase matches where its stem sequence
    appears as consecutive ``\\w+`` words, in order, regardless of the
    punctuation/whitespace between them (so "3. Monitoring, segmentation"
    matches "3 Monitoring segmentation" stems). Returns merged,
    sorted ``(start, end)`` char ranges; empty when ``spec`` has no
    phrases or none occur."""
    if not spec.phrases or not text:
        return []
    bounds = [(m.start(), m.end()) for m in re.finditer(r"\w+", text)]
    stems = [_stem(text[s:e]) for s, e in bounds]
    raw: list[tuple[int, int]] = []
    for phrase in spec.phrases:
        plist = list(phrase)
        n = len(plist)
        if n == 0:
            continue
        for i in range(len(stems) - n + 1):
            if stems[i : i + n] == plist:
                raw.append((bounds[i][0], bounds[i + n - 1][1]))
    if not raw:
        return []
    raw.sort()
    merged: list[tuple[int, int]] = [raw[0]]
    for start, end in raw[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def glob_match_mask(word: str, glob: str) -> list[bool] | None:
    """For ``word`` matched by a wildcard ``glob``, one bool per char: True where
    the char aligns to a LITERAL glob char (a "match"), False where ``*``/``?``
    filled it (a wildcard "variance"). None when the surface word doesn't fully
    match the glob (e.g. stemming dropped a suffix) — caller falls back to a
    whole-word highlight."""
    m = re.fullmatch(_glob_capture_regex(glob), word.lower())
    if m is None:
        return None
    fill: set[int] = set()
    for gi in range(1, (m.lastindex or 0) + 1):
        start, end = m.span(gi)
        fill.update(range(start, end))
    return [i not in fill for i in range(len(word))]


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

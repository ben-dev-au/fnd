"""Lower a :mod:`fnd.query_ast` tree into a Tantivy ``Query``.

Leaves reuse the existing resolvers — plain terms/phrases through the analyzer
(``parse_query`` for stemming parity), wildcards/fuzzy through the stemmed-
dictionary expanders (BM25-scored ``term_query`` ORs), regex/glob through
``regex_query``. Internal nodes become ``boolean_query`` clauses: ``AND``→Must,
``OR``/adjacency→Should, ``NOT``/``-``→MustNot, ``+``→Must (forced). A group with
no positive clause gets an implicit ``all_query`` Must so a pure-negative still
matches a document set to subtract from.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import tantivy
from tantivy import Occur, Query

from fnd.query_ast import (
    And,
    Boosted,
    Fuzzy,
    Node,
    Not,
    Or,
    Phrase,
    Regex,
    Required,
    Term,
    Wildcard,
)
from fnd.schema import F_BODY

if TYPE_CHECKING:
    from fnd.query import Searcher


def compile_query(
    node: Node | None,
    *,
    searcher: Searcher,
    schema: tantivy.Schema,
    parse_kwargs: dict[str, object],
) -> Query:
    """Compile ``node`` against ``searcher``'s index. ``parse_kwargs`` carries the
    body ``default_field_names`` (and any cascade ``fuzzy_fields``) used for plain
    term/phrase leaves so the auto-fuzzy pass still reaches them."""
    if node is None:
        return Query.empty_query()
    return _Compiler(searcher, schema, parse_kwargs).compile(node)


class _Compiler:
    def __init__(
        self, searcher: Searcher, schema: tantivy.Schema, parse_kwargs: dict[str, object]
    ) -> None:
        self._s = searcher
        self._schema = schema
        self._pk = parse_kwargs

    def compile(self, n: Node) -> Query:
        if isinstance(n, Term):
            return self._term(n.text)
        if isinstance(n, Phrase):
            return self._phrase(n)
        if isinstance(n, Wildcard):
            return self._wildcard(n)
        if isinstance(n, Fuzzy):
            return self._fuzzy(n)
        if isinstance(n, Regex):
            return self._regex(n.pattern)
        if isinstance(n, Boosted):
            return Query.boost_query(self.compile(n.child), n.factor)
        if isinstance(n, And):
            return self._assemble(n.children, Occur.Must)
        if isinstance(n, Or):
            return self._assemble(n.children, Occur.Should)
        if isinstance(n, Not):  # standalone negation → subtract from everything
            return Query.boolean_query(
                [(Occur.Must, Query.all_query()), (Occur.MustNot, self.compile(n.child))]
            )
        # Required (the only remaining node) — its child carries the query; the
        # forced-Must occur is applied by the enclosing group in _assemble.
        return self.compile(n.child)

    # ── group assembly ──────────────────────────────────────────────
    def _assemble(self, children: tuple[Node, ...], base: Occur) -> Query:
        clauses: list[tuple[Occur, Query]] = []
        positives = 0
        for c in children:
            if isinstance(c, Not):  # ``-x`` / ``NOT x`` excludes regardless of group
                clauses.append((Occur.MustNot, self.compile(c.child)))
            elif isinstance(c, Required):  # ``+x`` forces required regardless of group
                clauses.append((Occur.Must, self.compile(c.child)))
                positives += 1
            else:
                clauses.append((base, self.compile(c)))
                positives += 1
        if positives == 0:  # pure-negative group needs a base to subtract from
            clauses.insert(0, (Occur.Must, Query.all_query()))
        if len(clauses) == 1 and clauses[0][0] is not Occur.MustNot:
            return clauses[0][1]  # don't wrap a lone positive (keeps BM25 parity)
        return Query.boolean_query(clauses)

    # ── leaves ──────────────────────────────────────────────────────
    def _term(self, text: str) -> Query:
        from fnd.query import _parse_query  # lazy: avoids import cycle

        return _parse_query(self._s._index, text, **self._pk)

    def _phrase(self, n: Phrase) -> Query:
        words = n.text.split()
        if any("*" in w or "?" in w for w in words):
            # A wildcard inside the phrase: parse_query silently drops ``*`` and
            # would match the bare (un-indexed) literal, so compile a positional
            # regex phrase instead — wildcard words become a glob regex, plain
            # words are stemmed to F_BODY token form (analyzer parity). Like the
            # other wildcard/fuzzy leaves, this matches F_BODY only.
            return self._wildcard_phrase(words, n.slop)
        from fnd.query import _parse_query

        q = f'"{n.text}"~{n.slop}' if n.slop else f'"{n.text}"'
        return _parse_query(self._s._index, q, **self._pk)

    def _wildcard_phrase(self, words: list[str], slop: int) -> Query:
        from fnd.matching import glob_to_regex
        from fnd.query_resolvers import fuzzy_stem

        patterns: list[str | tuple[int, str]] = []
        for w in words:
            if "*" in w or "?" in w:
                patterns.append(glob_to_regex(w))
            else:
                # Match the en_stem analyzer: it splits on every non-alphanumeric
                # char (hyphen, underscore, …) and stems each token, so a
                # punctuated word like ``cross-entropy`` occupies one phrase
                # position per sub-token. ``[\W_]`` splits on punctuation AND
                # underscore while keeping Unicode letters/digits intact.
                patterns.extend(re.escape(fuzzy_stem(sw)) for sw in re.split(r"[\W_]+", w) if sw)
        if not patterns:
            return Query.empty_query()
        try:
            return Query.regex_phrase_query(self._schema, F_BODY, patterns, slop=slop)
        except ValueError:
            return Query.empty_query()  # malformed glob contributes nothing

    def _wildcard(self, n: Wildcard) -> Query:
        from fnd.query_resolvers import prefix_variants, term_or_query

        if n.prefix is not None:  # ``crypto*`` → fast prefix scan
            q = term_or_query(self._schema, prefix_variants(self._s, n.prefix))
            return q if q is not None else Query.empty_query()
        from fnd.matching import glob_to_regex  # infix/leading glob → regex

        return self._regex(glob_to_regex(n.token))

    def _fuzzy(self, n: Fuzzy) -> Query:
        from fnd.matching import auto_fuzzy_distance
        from fnd.query_resolvers import fuzzy_stem, fuzzy_variants, term_or_query

        stem = fuzzy_stem(n.term)
        dist = n.distance if n.distance is not None else auto_fuzzy_distance(stem)
        q = term_or_query(self._schema, fuzzy_variants(self._s, stem, dist))
        return q if q is not None else Query.empty_query()

    def _regex(self, pattern: str) -> Query:
        # F_BODY tokens are lowercased (en_stem), and the pattern is kept verbatim
        # (no destructive lowercasing), so match case-insensitively via ``(?i)``.
        try:
            return Query.regex_query(self._schema, F_BODY, f"(?i){pattern}")
        except ValueError:
            return Query.empty_query()  # malformed regex/glob contributes nothing

"""Lower a :mod:`fnd.query_ast` tree into a Tantivy ``Query``.

Leaves reuse the existing resolvers: plain terms/phrases through the analyser
(``parse_query`` for stemming parity), fuzzy through the stem dictionary and
wildcards/regexes through the word dictionary (BM25-scored ``term_query`` ORs,
see :mod:`fnd.query_resolvers`). Internal nodes become ``boolean_query`` clauses: ``AND``→Must,
``OR``/adjacency→Should, ``NOT``/``-``→MustNot, ``+``→Must (forced). A group with
no positive clause gets an implicit ``all_query`` Must so a pure-negative still
matches a document set to subtract from.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import tantivy
from tantivy import Occur, Query

from fnd.analysis import fold, index_token
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
    fuzzy_word,
    is_phrase_pattern,
)
from fnd.query_errors import QuerySyntaxError
from fnd.query_escape import literal, literal_phrase
from fnd.query_fields import resolve
from fnd.query_filters import compile_clause
from fnd.schema import F_BODY, F_WORDS

if TYPE_CHECKING:
    from fnd.query import Searcher

_FIELD_CLAUSE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):")


def _parse_query(index: tantivy.Index, query: str, **kwargs: object) -> Query:
    """Parse via Tantivy, converting its raw ``ValueError`` syntax errors into a
    typed :class:`QuerySyntaxError` so callers never crash on a malformed query."""
    try:
        return index.parse_query(query, **kwargs)  # type: ignore[arg-type]
    except ValueError as e:
        raise QuerySyntaxError(
            "invalid query syntax",
            hint="check quotes, brackets and parentheses are balanced",
        ) from e


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
            return self._ranked_pattern(fold(n.pattern), glob=False)
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
        # A clause on a known field compiles as its filter would; anything else is text.
        clause = _FIELD_CLAUSE.match(text)
        if clause and resolve(clause.group(1)):
            name, value = clause.group(1), text[clause.end() :]
            compiled = compile_clause(name, value, self._schema, self._s._index)
            if compiled is None:
                raise QuerySyntaxError(
                    f"can't read the value of {name}:", hint="see the README for this field's form"
                )
            return compiled
        # Whitespace inside an atom comes from a quoted value (``TODO:"a b"``).
        escaped = literal_phrase(text) if any(ch.isspace() for ch in text) else literal(text)
        return _parse_query(self._s._index, escaped, **self._pk)

    def _phrase(self, n: Phrase) -> Query:
        words = n.text.split()
        if any(is_phrase_pattern(w) for w in words):
            # parse_query drops ``*`` and ``~``, so this is a positional regex phrase on F_BODY.
            return self._pattern_phrase(words, n.slop)
        return _parse_query(self._s._index, literal_phrase(n.text, slop=n.slop), **self._pk)

    def _pattern_phrase(self, words: list[str], slop: int) -> Query:
        if len(words) == 1:
            fuzzy = fuzzy_word(words[0])
            return self._fuzzy(fuzzy) if fuzzy else self._wildcard(Wildcard(words[0], None))
        patterns: list[str] = []
        for w in words:
            if is_phrase_pattern(w):
                member = self._phrase_member(fold(w))
                if member is None:
                    return Query.empty_query()
                patterns.append(member)
            else:
                # Match the index analyser: it splits on every non-alphanumeric
                # char (hyphen, underscore, …) and stems each token, so a
                # punctuated word like ``cross-entropy`` occupies one phrase
                # position per sub-token. ``[\W_]`` splits on punctuation AND
                # underscore while keeping Unicode letters/digits intact.
                patterns.extend(re.escape(index_token(sw)) for sw in re.split(r"[\W_]+", w) if sw)
        if not patterns:
            return Query.empty_query()
        if len(patterns) == 1:  # tantivy panics on a one-term regex phrase
            return self._regex(patterns[0], F_BODY)
        try:
            return Query.regex_phrase_query(self._schema, F_BODY, [*patterns], slop=slop)
        except ValueError:
            return Query.empty_query()  # malformed glob contributes nothing

    def _phrase_member(self, glob: str) -> str | None:
        """A phrase's pattern word as a regex over F_BODY stems, or None when it
        matches no word (see :func:`fnd.query_resolvers.phrase_member_stems`)."""
        from fnd.matching import glob_to_regex
        from fnd.query_resolvers import phrase_member_stems

        stems = phrase_member_stems(self._s, glob)
        if stems is None:
            return glob_to_regex(glob)
        if not stems:
            return None
        return "(?:" + "|".join(re.escape(st) for st in sorted(stems)) + ")"

    def _wildcard(self, n: Wildcard) -> Query:
        from fnd.query_resolvers import prefix_variants, term_or_query

        if n.prefix is not None:  # ``crypto*`` → fast prefix scan
            q = term_or_query(self._schema, prefix_variants(self._s, fold(n.prefix)), F_WORDS)
            return q if q is not None else Query.empty_query()
        from fnd.matching import glob_to_regex  # infix/leading glob → regex

        return self._ranked_pattern(glob_to_regex(fold(n.token)), glob=True)

    def _fuzzy(self, n: Fuzzy) -> Query:
        from fnd.matching import auto_fuzzy_distance
        from fnd.query_resolvers import fuzzy_variants, term_or_query

        stem = index_token(n.term)
        dist = n.distance if n.distance is not None else auto_fuzzy_distance(stem)
        q = term_or_query(self._schema, fuzzy_variants(self._s, stem, dist, front=True))
        return q if q is not None else Query.empty_query()

    def _ranked_pattern(self, pattern: str, *, glob: bool) -> Query:
        """Every match of ``pattern``, ranked by the terms it matched: a regex
        query alone scores each hit 1.0, leaving index order."""
        from fnd.query_resolvers import PATTERN_TERMS, blended_term_query, pattern_variants

        matched = self._regex(pattern)
        variants = pattern_variants(self._s, pattern, glob=glob) or {}
        docs = self._s._searcher.num_docs
        ranked = blended_term_query(self._schema, list(variants)[:PATTERN_TERMS], variants, docs)
        if ranked is None:
            return matched
        return Query.boolean_query(
            [(Occur.Must, Query.const_score_query(matched, 0.0)), (Occur.Should, ranked)]
        )

    def _regex(self, pattern: str, field: str = F_WORDS) -> Query:
        # Index tokens are lowercased and folded (fnd.analysis); the pattern is kept
        # verbatim, so match case-insensitively via ``(?i)``.
        try:
            return Query.regex_query(self._schema, field, f"(?i){pattern}")
        except ValueError:
            return Query.empty_query()  # malformed regex/glob contributes nothing

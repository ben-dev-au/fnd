"""Compile a tag selection into a typed tantivy query.

Tag values come from file content and Finder metadata, so they are untrusted
and cannot be validated upstream the way collection names are
(``validate_collection_name`` forbids exactly the characters that make
``fnd/query_dsl.py``'s raw collection interpolation safe).

Interpolating a tag into a query string is a real injection, not a theoretical
one: a frontmatter tag of ``evil" OR body:classified OR "`` yields
``tags_fm:"evil" OR body:classified OR ""``, which parses cleanly and returns
documents that do not carry the tag. Everything here builds Query objects
directly, so no escaping helper is needed and none can be forgotten.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import tantivy

from fnd.schema import TAG_FIELD_BY_SOURCE

__all__ = ["TagFilter", "compile_tag_filter"]


@dataclass(slots=True, frozen=True)
class TagFilter:
    """Selected tags, keyed by provider id.

    ``match_all`` applies to includes only; excludes always subtract.
    """

    include: dict[str, frozenset[str]] = field(default_factory=dict)
    exclude: dict[str, frozenset[str]] = field(default_factory=dict)
    match_all: bool = True

    def is_empty(self) -> bool:
        return not any(self.include.values()) and not any(self.exclude.values())


def _terms(selection: dict[str, frozenset[str]], schema: tantivy.Schema) -> list[tantivy.Query]:
    """One query per tag *value*, OR-ing across the sources it was selected in.

    Grouping by value matters: ``--tag recipe`` with both sources enabled
    fans out to {frontmatter: recipe, os: recipe}, and ANDing those would
    demand the same tag in frontmatter *and* Finder — which a real file
    essentially never satisfies. Distinct values stay separate, so
    ``match_all`` still ANDs genuinely different selections.

    Unknown sources are skipped so a provider added in a newer build can't
    break an older reader. Sorted for a deterministic query shape.
    """
    by_value: dict[str, list[str]] = {}
    for source in sorted(selection):
        field_name = TAG_FIELD_BY_SOURCE.get(source)
        if field_name is None:
            continue
        for value in sorted(selection[source]):
            by_value.setdefault(value, []).append(field_name)

    out: list[tantivy.Query] = []
    for value in sorted(by_value):
        fields = by_value[value]
        terms = [tantivy.Query.term_query(schema, f, value) for f in fields]
        out.append(
            terms[0]
            if len(terms) == 1
            else tantivy.Query.boolean_query([(tantivy.Occur.Should, t) for t in terms])
        )
    return out


def compile_tag_filter(spec: TagFilter, schema: tantivy.Schema) -> tantivy.Query | None:
    """One composite query, or None when nothing is selected.

    Returns a single query because the caller ANDs its filter list: an
    exclusion has to live *inside* this query as a MustNot clause rather than
    as a separate entry in that list.
    """
    includes = _terms(spec.include, schema)
    excludes = _terms(spec.exclude, schema)
    if not includes and not excludes:
        return None

    clauses: list[tuple[tantivy.Occur, tantivy.Query]] = []
    if includes:
        if spec.match_all:
            clauses.extend((tantivy.Occur.Must, q) for q in includes)
        elif len(includes) == 1:
            clauses.append((tantivy.Occur.Must, includes[0]))
        else:
            # Wrap the OR so it stays one Must clause and can't be weakened
            # by a sibling MustNot.
            clauses.append(
                (
                    tantivy.Occur.Must,
                    tantivy.Query.boolean_query([(tantivy.Occur.Should, q) for q in includes]),
                )
            )
    else:
        # Exclusion-only: a boolean query of nothing but MustNot matches
        # nothing, so anchor it on all docs.
        clauses.append((tantivy.Occur.Must, tantivy.Query.all_query()))

    clauses.extend((tantivy.Occur.MustNot, q) for q in excludes)
    return tantivy.Query.boolean_query(clauses)

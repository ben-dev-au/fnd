"""Query planning: the one place raw user text becomes a validated, search-ready
query. Both the CLI and the TUI build a :class:`QueryPlan` so they validate
identically (canonical Unicode, bounds, inline ``[metadata filter]`` split, and
proximity) instead of each re-deriving it with subtly different (and
inconsistent) error handling.

DSL *expansion* still happens downstream in :class:`fnd.query.Searcher`; the plan
carries the human lexical so highlight/match code keeps the user's own words.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from fnd.extract._limits import LIMIT_QUERY_BOOLEAN_TOKENS, LIMIT_QUERY_BYTES
from fnd.filter_dsl import quote_positions
from fnd.query_dsl import check_proximity, preprocess, split_metadata_filter
from fnd.query_errors import QuerySyntaxError, QueryTooLargeError
from fnd.query_spans import literal_spans, without_literals
from fnd.text_canon import canonical, quote_view

# Quotes (:func:`~fnd.text_canon.quote_view`) and full-width forms (``＊（）：``) as
# ASCII syntax, in search text only: a frontmatter value keeps them as typed.
# ``［］`` stay text unless they open a range after ``:``, else a word becomes a filter.
_ASCII_SYNTAX = str.maketrans(
    {0xFF01 + i: 0x21 + i for i in range(0x5E) if 0xFF01 + i not in (0xFF3B, 0xFF3D)}
    | {0x3000: " ", 0x2019: "'"}
)
# A range may mix bounds (``[lo TO hi}``), and ``｛｝`` are already ASCII here.
_FULL_WIDTH_RANGE = re.compile(r"(?<=:)([［{])([^［］{}\[\]]*)([］}])")
_RANGE_ENDS = str.maketrans("［］", "[]")


def search_text(lexical: str) -> str:
    """``lexical`` (search text, no ``[…]`` filter) with typographic and full-width
    syntax as ASCII."""
    text = quote_view(lexical).translate(_ASCII_SYNTAX)
    return _FULL_WIDTH_RANGE.sub(lambda m: m.group(0).translate(_RANGE_ENDS), text)


def _ascii_delimiters(rule: str) -> str:
    """``rule`` with the quotes that delimit its strings and quoted names as ASCII,
    read in :func:`~fnd.text_canon.quote_view`; every other character, an
    apostrophe or a quote inside a value, stays as typed."""
    view, out = quote_view(rule), list(rule)
    for at in quote_positions(view):
        out[at] = view[at]
    return "".join(out)


def enforce_query_bounds(query: str) -> None:
    """Refuse pathological queries before they reach Tantivy."""
    if len(query.encode("utf-8")) > LIMIT_QUERY_BYTES:
        raise QueryTooLargeError(f"query exceeds {LIMIT_QUERY_BYTES}-byte limit")
    # Cheap upper bound on boolean depth: count AND/OR/NOT tokens. Tantivy's
    # parser tree explodes when these multiply; the cap is conservative but well
    # above any realistic human query. Pad with spaces so leading `NOT foo` /
    # trailing `foo AND` boundary cases still get counted.
    padded = f" {query} "
    boolean_tokens = sum(padded.count(op) for op in (" AND ", " OR ", " NOT "))
    if boolean_tokens > LIMIT_QUERY_BOOLEAN_TOKENS:
        raise QueryTooLargeError(
            f"query has {boolean_tokens} boolean operators; limit is {LIMIT_QUERY_BOOLEAN_TOKENS}"
        )


def _refuse_unbalanced_structure(lexical: str) -> None:
    """Parentheses that pair, and no AND / OR / NOT without the word it needs.
    Quoted and regex text is the user's own and is not read."""
    structure = without_literals(lexical)
    depth = 0
    for ch in structure:
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if depth < 0:
            break
    if depth != 0:
        raise QuerySyntaxError("unbalanced parentheses", hint="each ( needs a )")
    words = structure.replace("(", " ").replace(")", " ").split()
    if words and (words[0] in {"AND", "OR"} or words[-1] in {"AND", "OR", "NOT"}):
        raise QuerySyntaxError(
            "a dangling AND, OR or NOT", hint="put a word either side of AND and OR, and after NOT"
        )


def _refuse_unclosed_quote(query: str) -> None:
    for span in literal_spans(query):
        text = query[span.start : span.end]
        if text.startswith('"') and (len(text) == 1 or not text.endswith('"')):
            raise QuerySyntaxError("unclosed quote", hint='close the " or remove it')


@dataclass(frozen=True)
class QueryPlan:
    """A validated query: the filter-stripped lexical text the user typed plus
    the optional inline metadata-filter expression."""

    lexical: str
    metadata_filter: str | None

    @classmethod
    def from_user_text(cls, raw: str) -> QueryPlan:
        """Validate ``raw`` and split off any inline ``[…]`` filter.

        Raises :class:`QueryTooLargeError` (size/complexity) or
        :class:`QuerySyntaxError` (unbalanced brackets, malformed proximity).
        """
        raw = canonical(raw)
        enforce_query_bounds(raw)
        _refuse_unclosed_quote(quote_view(raw))
        try:
            lexical, metadata_filter = split_metadata_filter(raw)
        except ValueError as e:
            raise QuerySyntaxError(str(e), hint="check that [ ] brackets are balanced") from e
        lexical = search_text(lexical)
        _refuse_unclosed_quote(lexical)
        if metadata_filter is not None:
            metadata_filter = _ascii_delimiters(metadata_filter)
        # Validate proximity against the expanded form (well-formed {N} a b is
        # already "a b"~N, so a surviving brace is a real mistake).
        check_proximity(preprocess(lexical))
        _refuse_unbalanced_structure(lexical)
        return cls(lexical=lexical, metadata_filter=metadata_filter)

"""Query planning: the one place raw user text becomes a validated, search-ready
query. Both the CLI and the TUI build a :class:`QueryPlan` so they validate
identically — bounds, inline ``[metadata filter]`` split, and proximity — instead
of each re-deriving it with subtly different (and inconsistent) error handling.

DSL *expansion* still happens downstream in :class:`fnd.query.Searcher`; the plan
carries the human lexical so highlight/match code keeps the user's own words.
"""

from __future__ import annotations

from dataclasses import dataclass

from fnd.extract._limits import LIMIT_QUERY_BOOLEAN_TOKENS, LIMIT_QUERY_BYTES
from fnd.query_dsl import check_proximity, preprocess, split_metadata_filter
from fnd.query_errors import QuerySyntaxError, QueryTooLargeError


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
        enforce_query_bounds(raw)
        try:
            lexical, metadata_filter = split_metadata_filter(raw)
        except ValueError as e:
            raise QuerySyntaxError(str(e), hint="check that [ ] brackets are balanced") from e
        # Validate proximity against the expanded form (well-formed {N} a b is
        # already "a b"~N, so a surviving brace is a real mistake).
        check_proximity(preprocess(lexical))
        return cls(lexical=lexical, metadata_filter=metadata_filter)

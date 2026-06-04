"""Typed query errors shared across the DSL pre-pass, the query planner, and
the Tantivy boundary. Leaf module — no imports — so any layer can raise these
without risking an import cycle."""

from __future__ import annotations


class QueryError(ValueError):
    """Base for every recoverable problem with a user query."""


class QuerySyntaxError(QueryError):
    """A query the user can fix: malformed proximity, unbalanced brackets, or a
    string Tantivy's parser rejects. ``hint`` is a practical one-liner shown
    next to the query input — keep it calm and actionable."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class QueryTooLargeError(QueryError):
    """Query exceeds the size / complexity bounds in :mod:`fnd.extract._limits`.
    Defensive today — the local user is the only author — but pinned so any
    future URL-handler / Spotlight / ``--query-from-file`` path inherits it."""

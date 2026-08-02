"""Typed query errors shared across the DSL pre-pass, the query planner, and
the Tantivy boundary. Leaf module — no runtime imports — so any layer can raise
these without risking an import cycle."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


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


class UnknownFilterValueError(QueryError):
    """A filter value outside its closed set of legal values — a typo'd
    collection name, file kind, or date token.

    Carries the near-misses so the CLI can offer a correction instead of a
    bare refusal. ``flag`` names the option the value arrived on
    (``--collection``); ``None`` marks a token typed inside the query string
    itself, which the CLI reports but never rewrites.
    """

    def __init__(
        self,
        *,
        label: str,
        value: str,
        suggestions: Sequence[str] = (),
        known: Sequence[str] = (),
        flag: str | None = None,
    ) -> None:
        message = f"no {label} named {value!r}"
        super().__init__(message)
        self.message = message
        self.label = label
        self.value = value
        self.flag = flag
        self.suggestions = tuple(suggestions)
        self.known = tuple(known)

    @property
    def hint(self) -> str | None:
        """The "did you mean" one-liner, or None when nothing is close."""
        if not self.suggestions:
            return None
        joined = " or ".join(repr(s) for s in self.suggestions)
        return f"did you mean {joined}?"

    @property
    def correction(self) -> str | None:
        """The single unambiguous replacement, if there is exactly one."""
        return self.suggestions[0] if len(self.suggestions) == 1 else None


class QueryTooLargeError(QueryError):
    """Query exceeds the size / complexity bounds in :mod:`fnd.extract._limits`.
    Defensive today — the local user is the only author — but pinned so any
    future URL-handler / Spotlight / ``--query-from-file`` path inherits it."""

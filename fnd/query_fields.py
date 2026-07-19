"""Field registry: the one place that knows each query-facing field's tantivy
name, value type, and search context (scored vs hard filter).

Drives :mod:`fnd.query_filters`, which lowers ``field:value`` / ``c:name`` /
range clauses into typed tantivy queries. Centralises field knowledge that used
to be scattered across ``query.py`` (parse kwargs), ``query_dsl.py`` (regex
range/date expansion), and ``schema.py`` (tokenizers).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import Final

from fnd.query_dsl import _DATE_TOKEN_DAYS, FAR_FUTURE, _iso_to_ts, _now_ts
from fnd.schema import (
    F_AUTHOR,
    F_CHUNK_SEQ,
    F_COLLECTION,
    F_CREATED,
    F_HEADING_PATH,
    F_KIND,
    F_MTIME,
    F_PAGE,
    F_PATH_TOKENS,
    F_SLIDE,
    F_TITLE,
)


class FieldValue(Enum):
    """How a field's value is matched."""

    EXACT = auto()  # raw tokenizer; one untokenised term (kind, collection)
    TEXT = auto()  # default tokenizer (lowercased, unstemmed); term or phrase
    UINT = auto()  # numeric u64; point or range (page, slide, mtime, chunk_seq)


@dataclass(frozen=True)
class FieldSpec:
    """A query-facing field: its tantivy field, value kind, and (for UINT) the
    coercion from a query token to an integer bound."""

    query_name: str
    tantivy_field: str
    value: FieldValue
    coerce: Callable[[str], int] | None = None  # UINT only


def _coerce_uint(token: str) -> int:
    """Plain integer bound."""
    return int(token)


def _coerce_mtime(token: str) -> int:
    """``mtime`` bound: ISO date (YYYY[-MM[-DD]]) → unix, else a raw int."""
    t = token.strip()
    if t and t[0].isdigit() and "-" in t:
        return _iso_to_ts(t)
    return int(t)


# query-facing name → spec. Every entry here is a HARD FILTER (scored content
# lives in F_BODY and is handled by the parser, not this registry).
REGISTRY: Final[dict[str, FieldSpec]] = {
    "kind": FieldSpec("kind", F_KIND, FieldValue.EXACT),
    "collection": FieldSpec("collection", F_COLLECTION, FieldValue.EXACT),
    "title": FieldSpec("title", F_TITLE, FieldValue.TEXT),
    "author": FieldSpec("author", F_AUTHOR, FieldValue.TEXT),
    "heading_path": FieldSpec("heading_path", F_HEADING_PATH, FieldValue.TEXT),
    "path_tokens": FieldSpec("path_tokens", F_PATH_TOKENS, FieldValue.TEXT),
    "page": FieldSpec("page", F_PAGE, FieldValue.UINT, _coerce_uint),
    "slide": FieldSpec("slide", F_SLIDE, FieldValue.UINT, _coerce_uint),
    "chunk_seq": FieldSpec("chunk_seq", F_CHUNK_SEQ, FieldValue.UINT, _coerce_uint),
    "mtime": FieldSpec("mtime", F_MTIME, FieldValue.UINT, _coerce_mtime),
    "created": FieldSpec("created", F_CREATED, FieldValue.UINT, _coerce_mtime),
}

# ``c:`` is the collection shorthand.
ALIASES: Final[dict[str, str]] = {"c": "collection"}


def resolve(name: str) -> FieldSpec | None:
    """Return the spec for a query-facing field name (or alias), or None."""
    return REGISTRY.get(ALIASES.get(name, name))


def date_token_range(token: str) -> tuple[int, int] | None:
    """Map an ``mtime``/``created`` keyword (today/yesterday/week/month/year) to a
    ``(low, high)`` unix range, or None if not a known token.

    ``today`` means "since UTC midnight today" (modified today), not "since this
    exact instant" — the latter would match nothing. week/month/year are the
    documented "within the last N days" cumulative windows.
    """
    if token not in _DATE_TOKEN_DAYS:
        return None
    now = _now_ts()
    if token == "today":  # noqa: S105 — keyword, not a secret
        return (now - now % 86_400, FAR_FUTURE)  # UTC midnight today
    return (now - _DATE_TOKEN_DAYS[token] * 86_400, FAR_FUTURE)


# Kept for callers predating the rename; created: shares the same tokens.
mtime_token_range = date_token_range

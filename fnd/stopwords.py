"""Shared stopword set and query-time stopword stripping.

Function words carry ~zero IDF. They matter inside quoted phrases (so the
phrase still matches) but a bare-word query that overlaps a chunk *only*
via a stopword is pure noise — the chunk ranks at the tail yet is still
retrieved. :func:`strip_query_stopwords` drops standalone stopwords from a
plain bag-of-words query so retrieval gates on content terms. It bails on
any query carrying special syntax (quotes, field qualifiers, booleans,
ranges, proximity, wildcards) so those paths are left untouched.
"""

from __future__ import annotations

import re
from typing import Final

# Function words with ~zero IDF. Reused by the preview highlighter
# (:mod:`fnd.render`) and the query-time stripper below.
STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "nor",
        "of",
        "to",
        "in",
        "on",
        "at",
        "for",
        "from",
        "by",
        "with",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "into",
        "than",
        "then",
        "so",
        "such",
        "not",
        "no",
    }
)

# Any of these means the query carries explicit syntax (phrase, field
# qualifier, boolean/required term, range, proximity/fuzzy, wildcard) — we
# leave it verbatim so stripping can never break it. ``-`` / ``+`` only count
# as operators at a token boundary so hyphenated words ("defence-in-depth")
# stay plain.
_SPECIAL = re.compile(r"""["':()\[\]{}~*?]|\b(?:AND|OR|NOT)\b|(?:^|\s)[+\-]\S""")


def strip_query_stopwords(query: str) -> str:
    """Drop standalone stopwords from a plain bag-of-words query.

    Returns ``query`` unchanged when it carries special syntax, or when it
    is entirely stopwords (so an all-stopword query falls back to its
    original behaviour instead of becoming empty).
    """
    if _SPECIAL.search(query):
        return query
    kept = [t for t in query.split() if t.lower() not in STOPWORDS]
    if not kept:
        return query
    return " ".join(kept)

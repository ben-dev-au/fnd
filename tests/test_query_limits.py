"""Query size / complexity caps. (S4)

Pinned not because today's TUI user can DoS themselves, but because the
moment we add a URL handler, Spotlight integration, or
``--query-from-file`` the same parser becomes attacker-reachable.
"""

from __future__ import annotations

import pytest

from fnd.extract._limits import LIMIT_QUERY_BOOLEAN_TOKENS, LIMIT_QUERY_BYTES
from fnd.query import QueryTooLargeError, enforce_query_bounds


def test_normal_query_passes() -> None:
    enforce_query_bounds("entropy AND cross-entropy NOT regression")


def test_byte_limit_rejected() -> None:
    big = "a " * (LIMIT_QUERY_BYTES)
    with pytest.raises(QueryTooLargeError, match="byte limit"):
        enforce_query_bounds(big)


def test_boolean_depth_rejected() -> None:
    deep = " AND ".join(["x"] * (LIMIT_QUERY_BOOLEAN_TOKENS + 5))
    with pytest.raises(QueryTooLargeError, match="boolean"):
        enforce_query_bounds(deep)


def test_query_with_max_booleans_just_under_passes() -> None:
    # Exactly at limit should still pass; over-by-one should fail.
    just_under = " AND ".join(["x"] * LIMIT_QUERY_BOOLEAN_TOKENS)
    enforce_query_bounds(just_under)
    over = " AND ".join(["x"] * (LIMIT_QUERY_BOOLEAN_TOKENS + 2))
    with pytest.raises(QueryTooLargeError):
        enforce_query_bounds(over)

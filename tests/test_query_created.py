"""created: mirrors mtime: as a filter field."""

from __future__ import annotations

import pytest

from fnd.query_dsl import preprocess
from fnd.query_fields import resolve
from fnd.schema import F_CREATED


def test_created_resolves_to_the_index_field() -> None:
    spec = resolve("created")
    assert spec is not None
    assert spec.tantivy_field == F_CREATED


@pytest.mark.parametrize("token", ["today", "week", "month", "year"])
def test_created_tokens_expand_to_ranges(token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fnd.query_dsl._now_ts", lambda: 1_700_000_000)
    out = preprocess(f"created:{token}")
    assert "created:[" in out
    assert " TO " in out


def test_created_expansion_is_independent_of_mtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fnd.query_dsl._now_ts", lambda: 1_700_000_000)
    out = preprocess("created:week mtime:year")
    assert out.count("created:[") == 1
    assert out.count("mtime:[") == 1


def test_unknown_token_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fnd.query_dsl._now_ts", lambda: 1_700_000_000)
    assert "created:banana" in preprocess("created:banana")


def test_mtime_token_range_alias_still_works() -> None:
    """Kept so callers predating the rename don't break."""
    from fnd.query_fields import date_token_range, mtime_token_range

    assert mtime_token_range("week") is not None
    assert mtime_token_range("week") == date_token_range("week")

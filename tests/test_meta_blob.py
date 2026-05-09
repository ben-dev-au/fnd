"""Phase 5.5e-2: JSON-roundtrip of frontmatter dicts for query-time filter."""

from __future__ import annotations

import datetime as dt

import pytest

from acorn.meta_blob import decode, encode


def test_empty_dict_roundtrip() -> None:
    assert decode(encode({})) == {}


def test_string_int_float_roundtrip() -> None:
    fm = {"Course": "DPwC", "priority": 3, "weight": 1.5}
    assert decode(encode(fm)) == fm


def test_bool_and_none_roundtrip() -> None:
    fm = {"archived": False, "active": True, "parent": None}
    out = decode(encode(fm))
    assert out == fm
    assert out["archived"] is False
    assert out["active"] is True
    assert out["parent"] is None


def test_list_roundtrip() -> None:
    fm = {"tags": ["course", "active"], "vals": [1, 2.5, True, None]}
    assert decode(encode(fm)) == fm


def test_date_roundtrip() -> None:
    """Dates must round-trip as ``datetime.date`` so the DSL evaluator's
    ordered comparisons (`<=`, `>=`) work — strings can't be compared
    against dates and silently fail closed."""
    fm = {"due": dt.date(2026, 6, 1)}
    out = decode(encode(fm))
    assert out == fm
    assert isinstance(out["due"], dt.date)


def test_date_inside_list_roundtrip() -> None:
    fm = {"deadlines": [dt.date(2026, 6, 1), dt.date(2026, 7, 1)]}
    out = decode(encode(fm))
    assert all(isinstance(d, dt.date) for d in out["deadlines"])


def test_decode_empty_bytes_returns_empty_dict() -> None:
    """Non-md chunks store empty bytes; decode must map this to ``{}`` so
    callers don't need to special-case the empty-file path."""
    assert decode(b"") == {}


def test_encode_returns_bytes() -> None:
    blob = encode({"x": 1})
    assert isinstance(blob, bytes)


def test_encode_unsupported_type_raises() -> None:
    """Sets / arbitrary objects aren't supported. Frontmatter only ever
    yields the JSON-friendly types we serialize, so anything else is a
    programming bug."""
    with pytest.raises(TypeError):
        encode({"weird": {1, 2, 3}})

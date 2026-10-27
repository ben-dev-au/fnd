"""JSON-roundtrip of frontmatter dicts for query-time filter."""

from __future__ import annotations

import datetime as dt

import pytest

from fnd.meta_blob import decode, encode


def test_empty_dict_roundtrip() -> None:
    assert decode(encode({})) == {}


def test_string_int_float_roundtrip() -> None:
    fm = {"Project": "Website", "priority": 3, "weight": 1.5}
    assert decode(encode(fm)) == fm


def test_bool_and_none_roundtrip() -> None:
    fm = {"archived": False, "active": True, "parent": None}
    out = decode(encode(fm))
    assert out == fm
    assert out["archived"] is False
    assert out["active"] is True
    assert out["parent"] is None


def test_list_roundtrip() -> None:
    fm = {"tags": ["project", "active"], "vals": [1, 2.5, True, None]}
    assert decode(encode(fm)) == fm


def test_date_roundtrip() -> None:
    """Dates must round-trip as ``datetime.date`` so the DSL evaluator's
    ordered comparisons (`<=`, `>=`) work: strings can't be compared
    against dates and silently fail closed."""
    fm = {"due": dt.date(2026, 6, 1)}
    out = decode(encode(fm))
    assert out == fm
    assert isinstance(out["due"], dt.date)


def test_date_inside_list_roundtrip() -> None:
    fm = {"deadlines": [dt.date(2026, 6, 1), dt.date(2026, 7, 1)]}
    out = decode(encode(fm))
    assert all(isinstance(d, dt.date) for d in out["deadlines"])


@pytest.mark.parametrize(
    "fm",
    [
        {"__type__": "date"},
        {"__type__": "date", "value": "notadate"},
        {"__type__": "date", "value": "2020-01-01"},
        {"__type__": "date", "value": dt.date(2020, 1, 1)},
        {"__type__": "date", "value": [dt.date(2020, 1, 1)]},
    ],
)
def test_frontmatter_type_key_roundtrips_as_a_key(fm: dict[str, object]) -> None:
    """A note's own ``__type__`` key is data, never a date envelope."""
    assert decode(encode(fm)) == fm


def test_decode_reads_blobs_already_in_the_index() -> None:
    """Blobs as the encoder writes them decode to the same dates."""
    blob = (
        b'{"due": {"__type__": "date", "value": "2026-06-01"}, '
        b'"deadlines": [{"__type__": "date", "value": "2026-07-01"}], '
        b'"nested": [["x", {"__type__": "date", "value": "2026-08-01"}]], '
        b'"old": {"__type__": "date", "value": "0999-01-01"}}'
    )
    fm = {
        "due": dt.date(2026, 6, 1),
        "deadlines": [dt.date(2026, 7, 1)],
        "nested": [["x", dt.date(2026, 8, 1)]],
        "old": dt.date(999, 1, 1),
    }
    assert encode(fm) == blob
    assert decode(blob) == fm


def test_decode_deep_list_nesting_needs_no_recursion() -> None:
    """List nesting far past the recursion limit decodes as the JSON scanner parses it."""
    depth = 2000
    envelope = '{"__type__": "date", "value": "2026-06-01"}'
    blob = ('{"deep": ' + "[" * depth + envelope + "]" * depth + "}").encode()
    expected: object = dt.date(2026, 6, 1)
    for _ in range(depth):
        expected = [expected]
    assert decode(blob) == {"deep": expected}


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

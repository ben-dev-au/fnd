"""Frontmatter ↔ JSON bytes.

The Tantivy ``meta_blob`` stored field holds JSON-encoded frontmatter so
the query-time post-filter can apply the same DSL predicate the indexer
already uses. JSON doesn't natively round-trip ``datetime.date``,
so we wrap dates in a small typed envelope::

    encode({"due": date(2026, 6, 1)}) →
        b'{"due": {"__type__": "date", "value": "2026-06-01"}}'

The DSL evaluator needs ``dt.date`` instances on both sides for ordered
comparisons (the :func:`fnd.filter_dsl._orderable` helper rejects
str-vs-date), so the round-trip is load-bearing, not just cosmetic.

Frontmatter values are only scalars, dates and lists, so the only dicts in
a blob are the top-level mapping and envelopes. The decoder never reads the
top-level mapping as an envelope, since its keys are the note's own (a
``__type__`` key included), and restores only a dict shaped exactly as
:func:`_default` writes it. Lists are walked with an explicit stack, so
nesting depth costs no Python recursion.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

_TYPE_KEY = "__type__"


def encode(fm: dict[str, Any]) -> bytes:
    """Encode a frontmatter dict to JSON bytes. Raises TypeError for any
    value that isn't a JSON primitive, list of primitives, or
    ``datetime.date``."""
    return json.dumps(fm, default=_default).encode("utf-8")


def decode(blob: bytes) -> dict[str, Any]:
    """Decode bytes back into a frontmatter dict. Empty bytes map to an
    empty dict (the no-frontmatter case for non-md chunks)."""
    if not blob:
        return {}
    nested: list[list[Any]] = []
    fm = {key: _restore(value, nested) for key, value in json.loads(blob.decode("utf-8")).items()}
    while nested:
        items = nested.pop()
        for i, value in enumerate(items):
            items[i] = _restore(value, nested)
    return fm


def _default(o: object) -> object:
    if isinstance(o, dt.date) and not isinstance(o, dt.datetime):
        return {_TYPE_KEY: "date", "value": o.isoformat()}
    raise TypeError(f"unsupported type for meta_blob: {type(o).__name__}")


def _restore(value: object, nested: list[list[Any]]) -> object:
    """``value`` with an envelope turned back into its date; a list is queued on ``nested``."""
    if isinstance(value, list):
        nested.append(value)
    elif isinstance(value, dict):
        return _envelope_date(value)
    return value


def _envelope_date(d: dict[str, Any]) -> object:
    """The date ``d`` holds if it is exactly a :func:`_default` envelope, else ``d`` unchanged."""
    value = d.get("value")
    if d.keys() == {_TYPE_KEY, "value"} and d[_TYPE_KEY] == "date" and isinstance(value, str):
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            pass
    return d

"""Frontmatter ↔ JSON bytes.

The Tantivy ``meta_blob`` stored field holds JSON-encoded frontmatter so
the query-time post-filter can apply the same DSL predicate the indexer
already uses. JSON doesn't natively round-trip ``datetime.date``,
so we wrap dates in a small typed envelope::

    encode({"due": date(2026, 6, 1)}) →
        b'{"due": {"__type__": "date", "value": "2026-06-01"}}'

The decoder restores them via a JSON ``object_hook``. The DSL evaluator
needs ``dt.date`` instances on both sides for ordered comparisons (the
:func:`fnd.filter_dsl._orderable` helper rejects str-vs-date), so the
round-trip is load-bearing — not just cosmetic.
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
    return json.loads(blob.decode("utf-8"), object_hook=_object_hook)


def _default(o: object) -> object:
    if isinstance(o, dt.date) and not isinstance(o, dt.datetime):
        return {_TYPE_KEY: "date", "value": o.isoformat()}
    raise TypeError(f"unsupported type for meta_blob: {type(o).__name__}")


def _object_hook(d: dict[str, Any]) -> object:
    t = d.get(_TYPE_KEY)
    if t == "date":
        return dt.date.fromisoformat(d["value"])
    return d

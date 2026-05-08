"""Query-DSL pre-pass.

Translates acorn shorthand into Tantivy QueryParser-compatible syntax.
Tantivy handles natively: phrase, boolean (AND/OR/NOT), fuzzy (``~N``), wildcards,
ranges (``[low TO high]``), and field-restricted queries.

This pre-pass adds, per plan §3 + §9:

* ``c:papers`` / ``c:papers,notes``       → ``collection:papers (OR ...)``
* ``mtime:today``/``yesterday``/``week``/
  ``month``/``year``                       → ``mtime:[unix-low TO unix-high]``
* ``mtime:>2024-01-01``                    → ``mtime:[unix-2024-01-01 TO 99999999999]``
* ``mtime:<2024-01-01``                    → ``mtime:[0 TO unix-2024-01-01]``
* ``slide:>5`` / ``page:>5``               → ``slide:[6 TO 99999999999]``
* ``slide:<5``                             → ``slide:[0 TO 4]``
* ``{N} a b c``                            → ``"a b c"~N`` (Foxtrot-style)
* ``a NEAR/N b``                           → ``"a b"~N``

The output is a single string that Tantivy's QueryParser can consume directly.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Final

# Far-future unix timestamp used as "no upper bound" in numeric ranges.
FAR_FUTURE: Final = 99_999_999_999  # year ~5138
# Minimum unix timestamp.
FAR_PAST: Final = 0

# Date tokens: keyword → number of days back from "now".
_DATE_TOKEN_DAYS: Final[dict[str, int]] = {
    "today": 0,
    "yesterday": 1,
    "week": 7,
    "month": 30,
    "year": 365,
}

# Numeric fields where comparison shorthand applies.
_NUMERIC_FIELDS: Final = frozenset({"page", "slide", "mtime", "chunk_seq"})


def _now_ts() -> int:
    """Current unix timestamp; abstracted so tests can monkeypatch."""
    return int(dt.datetime.now(tz=dt.UTC).timestamp())


def _iso_to_ts(iso: str) -> int:
    """Parse YYYY-MM-DD or YYYY into a unix timestamp at UTC midnight."""
    s = iso.strip()
    if re.fullmatch(r"\d{4}", s):
        s = f"{s}-01-01"
    elif re.fullmatch(r"\d{4}-\d{2}", s):
        s = f"{s}-01"
    d = dt.date.fromisoformat(s)
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.UTC).timestamp())


def _expand_collection_shorthand(q: str) -> str:
    """Translate ``c:papers`` and ``c:papers,notes`` into Tantivy form."""

    def repl(match: re.Match[str]) -> str:
        names = [n.strip() for n in match.group(1).split(",") if n.strip()]
        if len(names) == 1:
            return f'collection:"{names[0]}"'
        joined = " OR ".join(f'collection:"{n}"' for n in names)
        return f"({joined})"

    return re.sub(r"\bc:([A-Za-z0-9_,\-]+)", repl, q)


def _expand_date_token(q: str) -> str:
    """Translate ``mtime:today``/``yesterday``/``week``/``month``/``year``."""

    def repl(match: re.Match[str]) -> str:
        token = match.group(1)
        if token not in _DATE_TOKEN_DAYS:
            return match.group(0)
        days = _DATE_TOKEN_DAYS[token]
        now = _now_ts()
        low = now - days * 86_400
        return f"mtime:[{low} TO {FAR_FUTURE}]"

    return re.sub(r"\bmtime:([a-z]+)\b", repl, q)


def _expand_numeric_compare(q: str) -> str:
    """Translate ``field:>N`` / ``field:<N`` / ``field:>=N`` / ``field:<=N``
    on numeric fields into ``[low TO high]`` ranges. Also accepts ISO dates
    for ``mtime``."""
    pat = re.compile(r"\b(" + "|".join(_NUMERIC_FIELDS) + r"):(>=|<=|>|<)([\d\-]+)\b")
    date_like = re.compile(r"\d{4}(-\d{2}(-\d{2})?)?")

    def repl(match: re.Match[str]) -> str:
        field, op, value = match.group(1), match.group(2), match.group(3)
        try:
            n = _iso_to_ts(value) if field == "mtime" and date_like.fullmatch(value) else int(value)
        except ValueError:
            return match.group(0)

        if op == ">":
            return f"{field}:[{n + 1} TO {FAR_FUTURE}]"
        if op == ">=":
            return f"{field}:[{n} TO {FAR_FUTURE}]"
        if op == "<":
            return f"{field}:[{FAR_PAST} TO {n - 1}]"
        if op == "<=":
            return f"{field}:[{FAR_PAST} TO {n}]"
        return match.group(0)

    return pat.sub(repl, q)


def _expand_proximity_aliases(q: str) -> str:
    """Translate ``{N} a b c`` and ``a NEAR/N b`` into ``"... "~N``."""

    # `{N} a b c` form — the {N} prefix applies to the rest of the parenthesised
    # group or to the next quoted phrase / next bare-word run until a recognised
    # operator. Keep the rule simple: the prefix consumes through end-of-line
    # (or a closing paren), since chaining with other clauses is rare in
    # practice and users can wrap explicitly.
    def brace_repl(match: re.Match[str]) -> str:
        slop = int(match.group(1))
        rest = match.group(2).strip()
        # If `rest` already contains operators, leave it alone — too risky.
        if re.search(r"\b(AND|OR|NOT)\b|[()]", rest):
            return match.group(0)
        # Strip surrounding quotes if user already quoted.
        rest = rest.strip('"').strip()
        return f'"{rest}"~{slop}'

    q = re.sub(r"\{(\d+)\}\s+([^()]+?)$", brace_repl, q)

    # `a NEAR/N b` form — strict: two single-word terms with NEAR/<N> between.
    q = re.sub(
        r"\b(\w+)\s+NEAR/(\d+)\s+(\w+)\b",
        lambda m: f'"{m.group(1)} {m.group(3)}"~{m.group(2)}',
        q,
    )
    return q


def preprocess(query: str) -> str:
    """Run all DSL translations in a stable order and return the result."""
    q = query.strip()
    q = _expand_collection_shorthand(q)
    q = _expand_date_token(q)
    q = _expand_numeric_compare(q)
    q = _expand_proximity_aliases(q)
    return q

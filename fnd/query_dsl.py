"""Query-DSL pre-pass.

Translates fnd shorthand into Tantivy QueryParser-compatible syntax.
Tantivy handles natively: phrase, boolean (AND/OR/NOT), fuzzy (``~N``), wildcards,
ranges (``[low TO high]``), and field-restricted queries.

This pre-pass adds:

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
    """Translate ``c:papers``, ``c:papers,notes``, ``c:"Soft Eng"``,
    and mixed ``c:papers,"Soft Eng",notes`` into Tantivy form.

    Names may be bare (alnum + ``_`` + ``-``) or quoted with ``"`` /
    ``'`` to carry spaces and other punctuation. Multi-name lists are
    joined with ``OR`` and wrapped in a single ``collection:`` clause
    group so they compose with the rest of the query."""

    # One name in the list: a quoted run or a bare token. Spelled as a
    # regex fragment so it can be inlined into the surrounding pattern.
    name_token = r"""(?:"([^"]+)"|'([^']+)'|([A-Za-z0-9_\-]+))"""  # noqa: S105 — regex, not a password
    pattern = re.compile(
        rf"\bc:({name_token}(?:\s*,\s*{name_token})*)",
    )
    name_re = re.compile(name_token)

    def repl(match: re.Match[str]) -> str:
        names: list[str] = []
        for m in name_re.finditer(match.group(1)):
            n = m.group(1) or m.group(2) or m.group(3)
            if n:
                names.append(n)
        if not names:
            return match.group(0)
        if len(names) == 1:
            return f'collection:"{names[0]}"'
        joined = " OR ".join(f'collection:"{n}"' for n in names)
        return f"({joined})"

    return pattern.sub(repl, q)


def _expand_date_token(q: str) -> str:
    """Translate ``mtime:``/``created:`` today/yesterday/week/month/year."""

    def repl(match: re.Match[str]) -> str:
        field, token = match.group(1), match.group(2)
        if token not in _DATE_TOKEN_DAYS:
            return match.group(0)
        days = _DATE_TOKEN_DAYS[token]
        now = _now_ts()
        low = now - days * 86_400
        return f"{field}:[{low} TO {FAR_FUTURE}]"

    return re.sub(r"\b(mtime|created):([a-z]+)\b", repl, q)


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


# A proximity run token: a quoted phrase, or a bare word that is neither a
# boolean operator nor a field qualifier (``word:``) and carries no paren or
# brace. Excluding braces stops a following ``{N}`` from being swallowed as a
# run word (``{0}{0}`` → ``"{0}"~0``), which left a brace inside quotes that the
# next pass re-expanded — breaking idempotency.
_RUN_TOKEN: Final = r'(?:"[^"]*"|(?:(?!(?:AND|OR|NOT)\b)(?![^\s()]*:)[^\s(){}]+))'  # noqa: S105 — regex, not a password
_BRACE_PROX: Final = re.compile(rf"\{{(\d+)\}}\s*((?:{_RUN_TOKEN})(?:\s+{_RUN_TOKEN})*)?")

# A residual brace group that is a proximity attempt (no ``TO`` — that would be
# a Tantivy exclusive range, which we leave alone).
_PROX_RESIDUAL: Final = re.compile(r"\{(?![^}]*\bTO\b)[^}]*\}")
_QUOTED_SPAN: Final = re.compile(r"\"[^\"]*\"|'[^']*'")


def _expand_proximity_aliases(q: str) -> str:
    """Translate ``{N} a b c`` and ``a NEAR/N b`` into ``"... "~N``.

    ``{N}`` binds to the immediately-following run of plain words / quoted
    phrases and stops at the first boolean operator, field qualifier, or
    parenthesis — the remainder is preserved verbatim. A ``{N}`` with no usable
    run is left in place for :func:`check_proximity` to flag.
    """

    def brace_repl(match: re.Match[str]) -> str:
        slop = int(match.group(1))
        run = (match.group(2) or "").strip()
        if not run:
            return match.group(0)  # nothing to bind to — leave for validation
        inner = " ".join(run.replace('"', " ").split())
        return f'"{inner}"~{slop}'

    q = _BRACE_PROX.sub(brace_repl, q)

    # `a NEAR/N b` form — strict: two single-word terms with NEAR/<N> between.
    q = re.sub(
        r"\b(\w+)\s+NEAR/(\d+)\s+(\w+)\b",
        lambda m: f'"{m.group(1)} {m.group(3)}"~{m.group(2)}',
        q,
    )
    return q


def check_proximity(expanded: str) -> None:
    """Raise :class:`QuerySyntaxError` if a proximity brace survived expansion.

    Runs on the *expanded* query: a well-formed ``{N} a b`` is already
    ``"a b"~N`` (no brace left), so anything matching here — ``{60}`` alone,
    ``{abc}``, ``{}``, ``{-5}`` — is a malformed proximity the user can fix.
    Braces inside quotes (literal text) and ``{lo TO hi}`` ranges are ignored.
    """
    from fnd.query_errors import QuerySyntaxError

    outside_quotes = _QUOTED_SPAN.sub(" ", expanded)
    if _PROX_RESIDUAL.search(outside_quotes):
        raise QuerySyntaxError(
            "malformed proximity",
            hint="proximity is {N} word word — a number in braces then two or more plain words",
        )


def preprocess(query: str) -> str:
    """Run all DSL translations in a stable order and return the result."""
    q = query.strip()
    q = _expand_collection_shorthand(q)
    q = _expand_date_token(q)
    q = _expand_numeric_compare(q)
    q = _expand_proximity_aliases(q)
    return q


def split_metadata_filter(query: str) -> tuple[str, str | None]:
    """Extract a single top-level ``[…]`` clause from ``query``.

    Returns ``(lexical_query, metadata_filter_or_None)``. ``[…]`` blocks
    appearing inside a quoted phrase are left intact, and a ``field:[lo TO hi]``
    range (the ``[`` follows a ``:``) is a numeric range — left in the lexical
    query, not treated as a filter. A filter may contain nested ``in [ … ]``
    lists. An empty ``[]`` is treated as no filter. Two or more top-level filter
    blocks raise ``ValueError`` — compose alternatives with ``AND``/``OR``
    inside the single block.

    Whitespace around the extracted clause is collapsed so the resulting
    lexical query reads naturally.
    """
    in_quote: str | None = None
    bracket_start: int | None = None
    depth = 0  # nesting inside the active filter ([... in [...] ...])
    found_range: tuple[int, int] | None = None
    i = 0
    while i < len(query):
        ch = query[i]
        if in_quote:
            if ch == in_quote:
                in_quote = None
            i += 1
            continue
        if ch in ('"', "'"):
            in_quote = ch
            i += 1
            continue
        if ch == "[":
            if bracket_start is None and i > 0 and query[i - 1] == ":":
                # field:[lo TO hi] — a numeric range, not a metadata filter.
                i += 1
                continue
            if bracket_start is None:
                bracket_start = i
                depth = 1
            else:
                depth += 1  # nested list inside the filter
            i += 1
            continue
        if ch == "]":
            if bracket_start is None:
                # Stray ']' (or a range's close) is part of the lexical query.
                i += 1
                continue
            depth -= 1
            if depth == 0:
                if found_range is not None:
                    raise ValueError("only one inline [metadata filter] clause per query")
                found_range = (bracket_start, i)
                bracket_start = None
            i += 1
            continue
        i += 1
    if bracket_start is not None:
        raise ValueError("unclosed [ in query")
    if found_range is None:
        return query, None
    start, end = found_range
    inner = query[start + 1 : end].strip()
    if not inner:
        # Empty []: drop it from the lexical, treat as "no filter".
        lex = (query[:start] + query[end + 1 :]).strip()
        lex = " ".join(lex.split())
        return lex, None
    lex = (query[:start] + query[end + 1 :]).strip()
    lex = " ".join(lex.split())
    return lex, inner

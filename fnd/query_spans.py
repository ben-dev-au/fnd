"""Where a query holds text that is not query syntax: quoted phrases and
``/regex/`` literals.

Every pass that rewrites or splits the query string (the ``[filter]`` split,
proximity expansion, filter extraction, the AST tokeniser, synonyms, the
highlighter) reads them from here, so a bracket inside a regex or the
apostrophe in ``don't`` means the same thing to all of them.

* A quote opens a phrase at the start of a token (after an optional ``+``/``-``)
  or straight after a field's ``:``. A ``"`` phrase ends at the next ``"``; a
  ``'`` phrase ends at a ``'`` that ends a token, so ``'rock'n'roll'`` is one
  phrase and the apostrophe inside a word never opens one. An unclosed ``"``
  runs to the end (the query plan refuses it); an unclosed ``'`` is an
  apostrophe (``'90s``), not a phrase.
* A ``/`` opens a regex at the start of a token and ends at an unescaped ``/``
  that ends a token. An unclosed one is not a regex, so ``/usr/bin`` is text.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Literal

__all__ = ["Span", "has_phrase", "literal_spans", "map_outside", "without_literals"]

type SpanKind = Literal["phrase", "regex"]

_OPENING_BRACKETS = "([,"
# What may follow a closing ``'`` or ``/``: the token ends, or a ``~N``/``^N``
# suffix or a closing bracket follows.
_CLOSING_MARKS = ")]},~^"


@dataclass(frozen=True, slots=True)
class Span:
    """``query[start:end]``, delimiters included."""

    start: int
    end: int
    kind: SpanKind


def _token_start(query: str, i: int) -> bool:
    j = i - 1
    if j >= 0 and query[j] in "+-":
        j -= 1
    return j < 0 or query[j].isspace() or query[j] in _OPENING_BRACKETS


def _ends_token(query: str, i: int) -> bool:
    return i + 1 >= len(query) or query[i + 1].isspace() or query[i + 1] in _CLOSING_MARKS


def _close(query: str, i: int, delim: str) -> int | None:
    """Index just past the closing ``delim`` for the opener at ``i``."""
    j = i + 1
    while j < len(query):
        ch = query[j]
        if delim == "/" and ch == "\\":
            j += 2
            continue
        if ch == delim and (delim == '"' or _ends_token(query, j)):
            return j + 1
        j += 1
    return None


def literal_spans(query: str) -> tuple[Span, ...]:
    """Every quoted phrase and regex literal in ``query``, in order."""
    spans: list[Span] = []
    # An opener that finds no closer means none after it can (one scan per
    # delimiter, not one per opener); escapes would shift ``/``'s scan.
    unclosed_from = {delim: len(query) + 1 for delim in "\"'/"}
    if "\\" in query:
        del unclosed_from["/"]

    def close(i: int, delim: str) -> int | None:
        if i >= unclosed_from.get(delim, len(query) + 1):
            return None
        end = _close(query, i, delim)
        if end is None and delim in unclosed_from:
            unclosed_from[delim] = i
        return end

    i = 0
    while i < len(query):
        ch = query[i]
        opens = _token_start(query, i) or (i > 0 and query[i - 1] == ":")
        if ch in "\"'" and opens:
            end = close(i, ch)
            if end is not None or ch == '"':
                spans.append(Span(i, end or len(query), "phrase"))
                i = end or len(query)
                continue
        if ch == "/" and _token_start(query, i):
            end = close(i, "/")
            if end is not None:
                spans.append(Span(i, end, "regex"))
                i = end
                continue
        i += 1
    return tuple(spans)


def map_outside(
    query: str,
    rewrite: Callable[[str], str],
    *,
    kinds: Collection[SpanKind] = ("phrase", "regex"),
) -> str:
    """``query`` with ``rewrite`` applied to the text between its literal spans
    of ``kinds``; those spans are kept verbatim."""
    out: list[str] = []
    last = 0
    for span in literal_spans(query):
        if span.kind not in kinds:
            continue
        out.append(rewrite(query[last : span.start]))
        out.append(query[span.start : span.end])
        last = span.end
    out.append(rewrite(query[last:]))
    return "".join(out)


def without_literals(query: str) -> str:
    """``query`` with each literal span as the placeholder word ``_``: still a
    word to a structural check, with none of its brackets or operators."""
    out = query
    for span in reversed(literal_spans(query)):
        out = f"{out[: span.start]} _ {out[span.end :]}"
    return out


def has_phrase(query: str) -> bool:
    """Whether ``query`` holds a quoted phrase, in either quote."""
    return any(span.kind == "phrase" for span in literal_spans(query))

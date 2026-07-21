"""Boolean query AST: parse the scored *content* string into a tree so every
operator (wildcard, fuzzy, regex, phrase, ``+``/``-``, ``^`` boost) composes
inside ``AND``/``OR``/``NOT`` and parentheses.

Tantivy's own ``parse_query`` is the only string→Query parser the binding
exposes, and it silently drops ``*`` and no-ops ``~N`` — so anything wrapped in
a boolean expression lost its wildcard/fuzzy. There is no native helper for the
boolean *structure* (``wildcard_query_to_regex_str`` only translates one term),
so we own the tree: parse here, lower each leaf with the existing resolvers in
:mod:`fnd.query_compile`, and assemble with ``boolean_query``.

Grammar (precedence low→high), implicit adjacency = ``OR`` (the weighted
default — bare multi-term OR-retrieves, BM25 ranks all-term docs higher)::

    or   := and ( (OR | <adjacent>) and )*
    and  := unary ( AND unary )*
    unary:= NOT unary | atom
    atom := '(' or ')' ['^'N] | ATOM            # ATOM carries +/- ^N ~N inline
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

# Leaf classification — mirrors fnd.query so the boolean path and the flat path
# recognise the same operator tokens.
_WILDCARD_RE: Final = re.compile(r"^(\w+)\*$")  # trailing prefix wildcard ``crypto*``
_FUZZY_RE: Final = re.compile(r"^(\w+)~(\d*)$")  # ``term~`` / ``term~N``
_REGEX_RE: Final = re.compile(r"^/(.+)/$")  # ``/pattern/``
_GLOB_RE: Final = re.compile(r"[*?]")  # any ``*``/``?`` → glob (infix/leading)
# A boost literal is a plain number (`^2`, `^1.5`) — kept strict so a malformed
# form like `foo^1.2.3` never reaches float() and crashes the query.
_NUMBER: Final = r"\d+(?:\.\d+)?"
_PHRASE_RE: Final = re.compile(rf"""^(['"])(.*)\1(?:~(\d+))?(?:\^({_NUMBER}))?$""", re.DOTALL)
_BOOST_RE: Final = re.compile(rf"\^({_NUMBER})$")
_KEYWORDS: Final = frozenset({"AND", "OR", "NOT"})


# ── AST nodes ────────────────────────────────────────────────────────
@dataclass(slots=True, frozen=True)
class Term:
    text: str


@dataclass(slots=True, frozen=True)
class Phrase:
    text: str  # space-joined words, unquoted
    slop: int


@dataclass(slots=True, frozen=True)
class Wildcard:
    token: str  # the raw glob, e.g. ``crypto*`` / ``*tion`` / ``col?r``
    prefix: str | None  # set only for a trailing ``word*`` (fast prefix scan)


@dataclass(slots=True, frozen=True)
class Fuzzy:
    term: str
    distance: int | None  # None → AUTO (length-derived)


@dataclass(slots=True, frozen=True)
class Regex:
    pattern: str


@dataclass(slots=True, frozen=True)
class Boosted:
    child: Node
    factor: float


@dataclass(slots=True, frozen=True)
class Not:
    child: Node


@dataclass(slots=True, frozen=True)
class Required:
    child: Node


@dataclass(slots=True, frozen=True)
class And:
    children: tuple[Node, ...]


@dataclass(slots=True, frozen=True)
class Or:
    children: tuple[Node, ...]


Node = Term | Phrase | Wildcard | Fuzzy | Regex | Boosted | Not | Required | And | Or


# ── Tokeniser ────────────────────────────────────────────────────────
# A token is (kind, value): LP RP AND OR NOT CARET ATOM.
def _tokenize(s: str) -> list[tuple[str, str]]:
    toks: list[tuple[str, str]] = []
    buf: list[str] = []
    n = len(s)
    i = 0

    def flush() -> None:
        if buf:
            t = "".join(buf)
            buf.clear()
            toks.append((t, t) if t in _KEYWORDS else ("ATOM", t))

    # A bare ``+``/``-`` already in the buffer is an operator prefix, not text,
    # so a following quote/regex still opens a phrase/regex atom (``-"a b"``).
    def prefix_only() -> bool:
        return not buf or buf == ["+"] or buf == ["-"]

    while i < n:
        ch = s[i]
        if ch.isspace():
            flush()
            i += 1
            continue
        if ch in "[{" and buf and buf[-1] == ":":
            # ``field:[lo TO hi]`` (inclusive) or ``field:{lo TO hi}``
            # (exclusive) — a range. The ``TO`` and its surrounding spaces are
            # part of the range syntax, so keep the whole bracket/brace run as
            # one atom (the leaf is handed to parse_query, which understands
            # ranges). Ranges may mix delimiters (``[lo TO hi}``), so close on
            # either ``]`` or ``}``. Without this the space inside would flush a
            # truncated ``field:[lo`` leaf that fails to parse.
            buf.append(ch)
            j = i + 1
            while j < n and s[j] not in "]}":
                buf.append(s[j])
                j += 1
            if j < n:  # include the closing delimiter
                buf.append(s[j])
                j += 1
            i = j
            continue
        if ch == "(":
            if buf and buf[-1] == ":":
                # ``field:(…)`` — keep the scoped group attached as one atom. The
                # AST has no field node, so the leaf is handed to parse_query
                # (which understands field syntax); other leaves around it
                # (wildcards/fuzzy) still compile normally.
                depth = 1
                buf.append(ch)
                j = i + 1
                while j < n and depth > 0:
                    if s[j] == "(":
                        depth += 1
                    elif s[j] == ")":
                        depth -= 1
                    buf.append(s[j])
                    j += 1
                i = j
                continue
            flush()
            toks.append(("LP", "("))
            i += 1
            continue
        if ch == ")":
            flush()
            toks.append(("RP", ")"))
            i += 1
            continue
        if ch in ("'", '"') and prefix_only():
            j = i + 1
            while j < n and s[j] != ch:
                j += 1
            j = min(j + 1, n)  # include closing quote (tolerate unterminated)
            j = _consume_suffix(s, j)  # trailing ~N / ^N
            buf.append(s[i:j])
            flush()
            i = j
            continue
        if ch == "/" and prefix_only():
            j = i + 1
            while j < n and s[j] != "/":
                j += 1
            j = min(j + 1, n)
            j = _consume_suffix(s, j)
            buf.append(s[i:j])
            flush()
            i = j
            continue
        if ch == "^" and not buf:
            # Standalone caret = boost on the just-closed group: ``(a OR b)^2``.
            # Only consume a well-formed number; a stray ``^`` is dropped.
            m = re.match(_NUMBER, s[i + 1 :])
            if m:
                toks.append(("CARET", m.group(0)))
                i += 1 + m.end()
            else:
                i += 1
            continue
        buf.append(ch)
        i += 1
    flush()
    return toks


def _consume_suffix(s: str, j: int) -> int:
    """Extend ``j`` over a trailing ``~<digits>`` and/or ``^<number>`` suffix."""
    m = re.match(r"~\d+", s[j:])
    if m:
        j += m.end()
    m = re.match(rf"\^{_NUMBER}", s[j:])
    if m:
        j += m.end()
    return j


# ── Parser (recursive descent) ───────────────────────────────────────
class _Parser:
    def __init__(self, toks: list[tuple[str, str]]) -> None:
        self._toks = toks
        self._pos = 0

    def _peek(self) -> tuple[str | None, str]:
        return self._toks[self._pos] if self._pos < len(self._toks) else (None, "")

    def _next(self) -> tuple[str | None, str]:
        t = self._peek()
        self._pos += 1
        return t

    def parse(self) -> Node | None:
        node = self._parse_or()
        # Ignore any dangling tokens (malformed tails) — best-effort.
        return node

    def _parse_or(self) -> Node | None:
        children = [self._parse_and()]
        while True:
            kind, _ = self._peek()
            if kind == "OR":
                self._next()
                children.append(self._parse_and())
            elif kind in ("ATOM", "LP", "NOT"):  # implicit adjacency = OR
                children.append(self._parse_and())
            else:
                break
        kept = [c for c in children if c is not None]
        if not kept:
            return None
        return kept[0] if len(kept) == 1 else Or(tuple(kept))

    def _parse_and(self) -> Node | None:
        children = [self._parse_unary()]
        while self._peek()[0] == "AND":
            self._next()
            children.append(self._parse_unary())
        kept = [c for c in children if c is not None]
        if not kept:
            return None
        return kept[0] if len(kept) == 1 else And(tuple(kept))

    def _parse_unary(self) -> Node | None:
        if self._peek()[0] == "NOT":
            self._next()
            child = self._parse_unary()
            return Not(child) if child is not None else None
        return self._parse_atom()

    def _parse_atom(self) -> Node | None:
        kind, value = self._peek()
        if kind == "LP":
            self._next()
            inner = self._parse_or()
            if self._peek()[0] == "RP":
                self._next()
            if self._peek()[0] == "CARET":
                _, cv = self._next()
                if inner is not None and cv:
                    return Boosted(inner, float(cv))
            return inner
        if kind == "ATOM":
            self._next()
            return _classify(value)
        # Operator / RP / CARET where an atom was expected → skip it.
        self._next()
        return None


def _classify(value: str) -> Node | None:
    """Turn one ATOM string into a leaf (or +/- wrapped leaf)."""
    if len(value) > 1 and value[0] == "+":
        inner = _classify(value[1:])
        return Required(inner) if inner is not None else None
    if len(value) > 1 and value[0] == "-":
        inner = _classify(value[1:])
        return Not(inner) if inner is not None else None

    pm = _PHRASE_RE.match(value)
    if pm:
        node: Node = Phrase(" ".join(pm.group(2).split()), int(pm.group(3)) if pm.group(3) else 0)
        return Boosted(node, float(pm.group(4))) if pm.group(4) else node

    boost: float | None = None
    bm = _BOOST_RE.search(value)
    if bm:
        boost = float(bm.group(1))
        value = value[: bm.start()]
    core = _classify_core(value)
    if core is None:
        return None
    return Boosted(core, boost) if boost is not None else core


def _classify_core(value: str) -> Node | None:
    rm = _REGEX_RE.match(value)
    if rm:
        return Regex(rm.group(1))  # verbatim — lowercasing corrupts \D/\B/named groups
    fm = _FUZZY_RE.match(value)
    if fm:
        return Fuzzy(fm.group(1), int(fm.group(2)) if fm.group(2) else None)
    wm = _WILDCARD_RE.match(value)
    if wm:
        return Wildcard(value, prefix=wm.group(1))
    if _GLOB_RE.search(value):
        return Wildcard(value, prefix=None)
    if not value:
        return None
    return Term(value)


def parse_query_ast(content: str) -> Node | None:
    """Parse a scored-content string into a boolean AST, or ``None`` when it
    carries no searchable atom (empty / only stray operators)."""
    toks = _tokenize(content)
    if not toks:
        return None
    return _Parser(toks).parse()

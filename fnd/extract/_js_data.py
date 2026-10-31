"""Tolerant reader for the data literals in inline scripts (nothing is executed).

``parse_assignments`` finds ``name = {...}`` / ``X.name = [...]`` literals and
returns them as Python values. Anything that is not plain data (calls,
functions, member access) becomes ``OPAQUE`` and the parse carries on, so a
page's computed cells never hide the literal data around them. Identifiers
that name an earlier ``var``/``let``/``const`` literal resolve to its value.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from typing import Final, NamedTuple

OPAQUE: Final = object()

_MAX_DEPTH = 40
_TOKEN = re.compile(
    r"""
    (?P<skip>\s+|//[^\n]*|/\*(?:.*?\*/|.*))
  | (?P<str>"(?:[^"\\\n]|\\.)*(?:"|(?=\n)|\Z)|'(?:[^'\\\n]|\\.)*(?:'|(?=\n)|\Z))
  | (?P<tpl>`(?:[^`\\]|\\.)*(?:`|\Z))
  | (?P<word>[\w$]+)
  | (?P<punct>[{}\[\](),:;])
  | (?P<other>.)
    """,
    re.S | re.X,
)
_ASSIGN = re.compile(r"(?:(?<![\w$])(\w+)\.(\w+)|\b(?:var|let|const)\s+(\w+))\s*=\s*[{\[]")
# What one script may expand its shared references to: nodes plus one unit per 16 characters.
_MAX_EXPANSION = 500_000
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
_ESCAPE = re.compile(r"\\(?:u([0-9a-fA-F]{4})|x([0-9a-fA-F]{2})|(.))", re.S)
_END = frozenset(",]});")
_SURROGATE = re.compile("[\ud800-\udfff]")


class Call(NamedTuple):
    """``name(arg, ...)`` with its arguments read as data; helper calls often build chips."""

    name: str
    args: tuple[object, ...]


class Assignment(NamedTuple):
    name: str
    value: object
    start: int  # char offsets of the literal in the source
    end: int
    base: str = ""  # the object of ``base.name = ...``; empty for a var/let/const


class _Tok(NamedTuple):
    kind: str
    text: str
    start: int
    end: int


def unescape(raw: str) -> str:
    def sub(m: re.Match[str]) -> str:
        digits = m.group(1) or m.group(2)
        return chr(int(digits, 16)) if digits else _ESCAPES.get(m.group(3), m.group(3))

    text = _ESCAPE.sub(sub, raw)
    if _SURROGATE.search(
        text
    ):  # a pair of \uXXXX escapes is one character; a lone half is not text
        text = text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    return text


def _tokens(src: str) -> list[_Tok]:
    out: list[_Tok] = []
    for m in _TOKEN.finditer(src):
        kind, text = m.lastgroup or "other", m.group()
        if kind in ("str", "tpl") and not _closed(text):
            kind = "other"  # an unterminated literal swallowed its line; keep it out of the data
        if kind != "skip":
            out.append(_Tok(kind, text, m.start(), m.end()))
    return out


def _closed(literal: str) -> bool:
    inner = literal[1:-1]
    return (
        len(literal) > 1
        and literal[-1] == literal[0]
        and (len(inner) - len(inner.rstrip("\\"))) % 2 == 0
    )


class _Expansion:
    """Size of the values a script refers to by name, so a shared reference cannot multiply."""

    def __init__(self) -> None:
        self.left = _MAX_EXPANSION
        self._weights: dict[int, int] = {}

    def weight(self, value: object) -> int:
        if isinstance(value, str):
            return 1 + len(value) // 16
        if not isinstance(value, (list, dict, Call)):
            return 1
        known = self._weights.get(id(value))
        if known is None:
            parts = (
                value.values()
                if isinstance(value, dict)
                else value.args
                if isinstance(value, Call)
                else value
            )
            known = min(1 + sum(self.weight(p) for p in parts), _MAX_EXPANSION + 1)
            self._weights[id(value)] = known
        return known

    def take(self, value: object) -> bool:
        """Charge one more use of ``value``; False once the script has expanded too far."""
        cost = self.weight(value)
        if cost > self.left:
            return False
        self.left -= cost
        return True


class _Parser:
    def __init__(
        self, toks: list[_Tok], env: dict[str, object], expansion: _Expansion | None = None
    ) -> None:
        self.toks = toks
        self.env = env
        self.expansion = expansion or _Expansion()
        self.i = 0

    def _peek(self) -> _Tok | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _at(self, text: str) -> bool:
        t = self._peek()
        return t is not None and t.kind in ("punct", "other") and t.text == text

    def _skip_expr(self) -> object:
        """Consume to the next depth-0 terminator; the expression is not data."""
        depth = 0
        while (t := self._peek()) is not None:
            if t.kind == "punct" and t.text in "([{":
                depth += 1
            elif t.kind == "punct" and t.text in ")]}":
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and t.kind == "punct" and t.text in ",;":
                break
            self.i += 1
        return OPAQUE

    def value(self, depth: int = 0) -> object:
        t = self._peek()
        if t is None or depth > _MAX_DEPTH:
            return self._skip_expr()
        v: object
        if t.kind == "str":
            self.i += 1
            v = unescape(t.text[1:-1])
        elif t.kind == "tpl":
            self.i += 1
            v = OPAQUE if "${" in t.text else unescape(t.text[1:-1])
        elif t.kind == "punct" and t.text == "[":
            v = self._array(depth + 1)
        elif t.kind == "punct" and t.text == "{":
            v = self._object(depth + 1)
        elif t.kind == "word":
            self.i += 1
            is_call = self._at("(") and not _NUMBER.fullmatch(t.text)
            v = self._call(t.text, depth + 1) if is_call else self._word(t.text)
        else:
            return self._skip_expr()
        nxt = self._peek()
        if nxt is None or (nxt.kind == "punct" and nxt.text in _END):
            return v
        if isinstance(v, str) and self._at("+"):
            self.i += 1
            rest = self.value(depth + 1)
            return v + rest if isinstance(rest, str) else OPAQUE
        return self._skip_expr()

    def _word(self, text: str) -> object:
        if _NUMBER.fullmatch(text):
            return float(text) if "." in text else int(text)
        if text in ("true", "false"):
            return text == "true"
        if text in ("null", "undefined"):
            return None
        value = self.env.get(text, OPAQUE)
        return value if self.expansion.take(value) else OPAQUE

    def _items(self, closer: str, depth: int) -> list[object]:
        self.i += 1
        items: list[object] = []
        while (t := self._peek()) is not None and not (t.kind == "punct" and t.text == closer):
            before = self.i
            items.append(self.value(depth))
            if self._at(",") or self.i == before:
                self.i += 1
        self.i += 1
        return items

    def _array(self, depth: int) -> list[object]:
        return self._items("]", depth)

    def _call(self, name: str, depth: int) -> Call:
        return Call(name, tuple(self._items(")", depth)))

    def _object(self, depth: int) -> dict[str, object]:
        self.i += 1
        obj: dict[str, object] = {}
        while (t := self._peek()) is not None and not (t.kind == "punct" and t.text == "}"):
            before = self.i
            key = unescape(t.text[1:-1]) if t.kind == "str" else t.text
            self.i += 1
            if t.kind in ("word", "str") and self._at(":"):
                self.i += 1
                obj[key] = self.value(depth)
            else:
                self.i = before + 1
                self._skip_expr()
            if self._at(",") or self.i == before:
                self.i += 1
        self.i += 1
        return obj


def parse_literal(src: str) -> object:
    """The value of a bare literal such as a ``<script type="application/json">`` body."""
    return _Parser(_tokens(src), {}).value()


def strings(src: str) -> list[tuple[int, str]]:
    """(offset, value) of every quoted string literal in ``src``."""
    return [(t.start, unescape(t.text[1:-1])) for t in _tokens(src) if t.kind == "str"]


def parse_assignments(src: str) -> list[Assignment]:
    """Literal values assigned in ``src``, in source order, outermost only."""
    toks = _tokens(src)
    starts = [t.start for t in toks]
    env: dict[str, object] = {}
    expansion = _Expansion()
    found: list[Assignment] = []
    last_end = 0
    for m in _ASSIGN.finditer(src):
        literal_at = m.end() - 1
        if literal_at < last_end:
            continue
        p = _Parser(toks, env, expansion)
        p.i = bisect_left(starts, literal_at)
        value = p.value()
        end = toks[min(p.i, len(toks)) - 1].end if toks else literal_at
        base, name = (m.group(1), m.group(2)) if m.group(2) else ("", m.group(3))
        if not base:
            env[name] = value
            expansion.weight(value)
        found.append(Assignment(name, value, literal_at, end, base))
        last_end = end
    return found

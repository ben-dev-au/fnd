"""Predicate DSL parser + evaluator (§5.5e-1).

Grammar::

    expr        ::= or_expr
    or_expr     ::= and_expr ( OR and_expr )*
    and_expr    ::= not_expr ( AND not_expr )*
    not_expr    ::= NOT? atom
    atom        ::= "(" expr ")" | comparison
    comparison  ::= ident OP value
                  | value "in" ident            (value is IN the field's list)
                  | value "not in" ident
                  | ident "in" "[" value,* "]"  (field's scalar is IN the list)
                  | ident "not in" "[" value,* "]"
    OP          ::= "==" | "!=" | "<" | ">" | "<=" | ">=" | "~~"
    value       ::= 'string' | "string" | number | iso_date | true | false | null
                    (numbers accept TOML-style separators: 50_000_000)
    ident       ::= word | "quoted word"

Same DSL is reused at query time (phase 5.5e-2) — the evaluator is
purely functional, takes a frontmatter dict, returns bool.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum, auto


class TokenKind(Enum):
    IDENT = auto()
    STRING = auto()
    NUMBER = auto()
    DATE = auto()
    OP = auto()
    AND = auto()
    OR = auto()
    NOT = auto()
    IN = auto()
    NOT_IN = auto()
    TRUE = auto()
    FALSE = auto()
    NULL = auto()
    LPAREN = auto()
    RPAREN = auto()
    LBRACKET = auto()
    RBRACKET = auto()
    COMMA = auto()
    EOF = auto()


@dataclass(slots=True, frozen=True)
class Token:
    kind: TokenKind
    value: object
    column: int  # 1-based column of the token's start


class FilterError(Exception):
    """Parse-time error with 1-based column + message. Used by both the
    config validator (where it converts to ValidationError) and the TUI
    form (where it surfaces inline as the user types)."""

    def __init__(self, message: str, column: int) -> None:
        super().__init__(f"col {column}: {message}")
        self.message = message
        self.column = column


_KEYWORDS = {
    "and": TokenKind.AND,
    "or": TokenKind.OR,
    "not": TokenKind.NOT,
    "in": TokenKind.IN,
    "true": TokenKind.TRUE,
    "false": TokenKind.FALSE,
    "null": TokenKind.NULL,
}


# Order matters: longer operators first so ``<=`` doesn't get tokenised
# as ``<`` then ``=``.
_OPERATORS = ("==", "!=", "<=", ">=", "~~", "<", ">")


_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# Separators sit between digits, as TOML requires: 50_000_000 but not 1__0 or 1_.
_NUMBER_RE = re.compile(r"\d+(?:_\d+)*(?:\.\d+(?:_\d+)*)?")
_BARE_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_\-.]*")


def _scan_string(text: str, start: int, quote: str) -> tuple[str, int] | None:
    """``(value, index past the closing quote)``, or None if unterminated.

    Backslash escapes the quote and itself. A trailing backslash is read as a
    literal instead when escaping it would run off the end, so a value that
    ends in one — a Windows path, say — still parses as it did before escapes
    existed.
    """
    for escaping in (True, False):
        parts: list[str] = []
        j = start + 1
        while j < len(text) and text[j] != quote:
            if escaping and text[j] == "\\" and j + 1 < len(text) and text[j + 1] in (quote, "\\"):
                parts.append(text[j + 1])
                j += 2
                continue
            parts.append(text[j])
            j += 1
        if j < len(text):
            return "".join(parts), j + 1
    return None


def tokenize(text: str) -> list[Token]:
    """Return the token stream ending with an EOF token. Raises FilterError
    on unterminated strings or unrecognised characters."""
    out: list[Token] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        col = i + 1
        if ch.isspace():
            i += 1
            continue
        if ch == "(":
            out.append(Token(TokenKind.LPAREN, "(", col))
            i += 1
            continue
        if ch == ")":
            out.append(Token(TokenKind.RPAREN, ")", col))
            i += 1
            continue
        if ch == "[":
            out.append(Token(TokenKind.LBRACKET, "[", col))
            i += 1
            continue
        if ch == "]":
            out.append(Token(TokenKind.RBRACKET, "]", col))
            i += 1
            continue
        if ch == ",":
            out.append(Token(TokenKind.COMMA, ",", col))
            i += 1
            continue
        # Operators (longest match first).
        matched_op = next((op for op in _OPERATORS if text.startswith(op, i)), None)
        if matched_op is not None:
            out.append(Token(TokenKind.OP, matched_op, col))
            i += len(matched_op)
            continue
        # Quoted tokens: double-quotes → IDENT (field names with spaces),
        # single-quotes → STRING (string literal values).
        if ch in ('"', "'"):
            # ``\'`` and ``\\`` escape, so a tag or field carrying a quote can
            # be written. Without it such a value had no text form at all and
            # was silently mangled on the way through.
            scanned = _scan_string(text, i, ch)
            if scanned is None:
                raise FilterError("unterminated string", col)
            value, i = scanned
            kind = TokenKind.IDENT if ch == '"' else TokenKind.STRING
            out.append(Token(kind, value, col))
            continue
        # Date literal (must precede number — same leading digits).
        date_match = _DATE_RE.match(text, i)
        if date_match:
            iso = date_match.group(0)
            try:
                value = dt.date.fromisoformat(iso)
            except ValueError as e:
                raise FilterError(f"invalid date {iso!r}", col) from e
            out.append(Token(TokenKind.DATE, value, col))
            i = date_match.end()
            continue
        num_match = _NUMBER_RE.match(text, i)
        if num_match:
            raw = num_match.group(0).replace("_", "")
            num_value: int | float = float(raw) if "." in raw else int(raw)
            out.append(Token(TokenKind.NUMBER, num_value, col))
            i = num_match.end()
            continue
        ident_match = _BARE_IDENT_RE.match(text, i)
        if ident_match:
            raw = ident_match.group(0)
            kw = _KEYWORDS.get(raw.lower())
            if kw is TokenKind.NOT and _peek_in(text, ident_match.end()):
                # ``not in`` collapses to NOT_IN; consume the ``in`` keyword.
                i = _consume_in_after_not(text, ident_match.end())
                out.append(Token(TokenKind.NOT_IN, "not in", col))
                continue
            if kw is not None:
                out.append(Token(kw, raw.lower(), col))
            else:
                out.append(Token(TokenKind.IDENT, raw, col))
            i = ident_match.end()
            continue
        raise FilterError(f"unexpected character {ch!r}", col)
    out.append(Token(TokenKind.EOF, "", n + 1))
    return out


def _peek_in(text: str, pos: int) -> bool:
    """True if the next non-whitespace token at ``pos`` is the keyword ``in``."""
    while pos < len(text) and text[pos].isspace():
        pos += 1
    m = _BARE_IDENT_RE.match(text, pos)
    return m is not None and m.group(0).lower() == "in"


def _consume_in_after_not(text: str, pos: int) -> int:
    """Skip whitespace and the ``in`` keyword, return the new index."""
    while pos < len(text) and text[pos].isspace():
        pos += 1
    m = _BARE_IDENT_RE.match(text, pos)
    assert m is not None  # _peek_in already checked
    assert m.group(0).lower() == "in"
    return m.end()


# ── AST nodes ─────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class Compare:
    """A field-vs-value comparison: ``Course == 'DPwC'``."""

    field: str
    op: str  # one of ==, !=, <, >, <=, >=, ~~
    value: object


@dataclass(slots=True, frozen=True)
class In:
    """Membership test: ``'course' in tags``. ``negated=True`` for ``not in``."""

    value: object
    field: str
    negated: bool


@dataclass(slots=True, frozen=True)
class FieldIn:
    """The field's scalar is one of a literal list: ``file.kind in ['pdf','md']``.

    The mirror image of :class:`In`, which tests a literal against a *list*
    field. A list-valued field never matches here; use ``In`` for that.
    """

    field: str
    values: tuple[object, ...]
    negated: bool


@dataclass(slots=True, frozen=True)
class And:
    left: object
    right: object


@dataclass(slots=True, frozen=True)
class Or:
    left: object
    right: object


@dataclass(slots=True, frozen=True)
class Not:
    operand: object


# ── Recursive-descent parser ──────────────────────────────────────


def parse(text: str) -> object:
    """Tokenize + parse into an AST. Raises FilterError on syntax issues
    with a 1-based column."""
    if not text.strip():
        raise FilterError("empty filter expression", 1)
    tokens = tokenize(text)
    parser = _Parser(tokens)
    tree = parser.parse_or()
    if parser.peek().kind is not TokenKind.EOF:
        raise FilterError(f"unexpected token {parser.peek().value!r}", parser.peek().column)
    return tree


class _Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self._tokens = tokens
        self._pos = 0

    def peek(self) -> Token:
        return self._tokens[self._pos]

    def advance(self) -> Token:
        t = self._tokens[self._pos]
        self._pos += 1
        return t

    def expect(self, kind: TokenKind) -> Token:
        t = self.peek()
        if t.kind is not kind:
            raise FilterError(f"expected {kind.name}, got {t.value!r}", t.column)
        return self.advance()

    # or_expr ::= and_expr ( OR and_expr )*
    def parse_or(self) -> object:
        left = self.parse_and()
        while self.peek().kind is TokenKind.OR:
            self.advance()
            right = self.parse_and()
            left = Or(left, right)
        return left

    # and_expr ::= not_expr ( AND not_expr )*
    def parse_and(self) -> object:
        left = self.parse_not()
        while self.peek().kind is TokenKind.AND:
            self.advance()
            right = self.parse_not()
            left = And(left, right)
        return left

    # not_expr ::= NOT? atom
    def parse_not(self) -> object:
        if self.peek().kind is TokenKind.NOT:
            self.advance()
            return Not(self.parse_atom())
        return self.parse_atom()

    # atom ::= "(" expr ")" | comparison
    def parse_atom(self) -> object:
        t = self.peek()
        if t.kind is TokenKind.LPAREN:
            self.advance()
            inner = self.parse_or()
            close = self.peek()
            if close.kind is not TokenKind.RPAREN:
                raise FilterError("expected closing paren )", close.column)
            self.advance()
            return inner
        return self.parse_comparison()

    def parse_comparison(self) -> object:
        first = self.peek()
        # Form A: ident OP value
        if first.kind is TokenKind.IDENT:
            self.advance()
            op_tok = self.peek()
            if op_tok.kind is TokenKind.OP:
                self.advance()
                value = self._parse_value()
                return Compare(str(first.value), str(op_tok.value), value)
            if op_tok.kind in (TokenKind.IN, TokenKind.NOT_IN):
                self.advance()
                values = self._parse_value_list()
                return FieldIn(str(first.value), values, negated=op_tok.kind is TokenKind.NOT_IN)
            raise FilterError(f"expected operator after {first.value!r}", op_tok.column)
        # Form C: value ("in"|"not in") ident
        if first.kind in (
            TokenKind.STRING,
            TokenKind.NUMBER,
            TokenKind.DATE,
            TokenKind.TRUE,
            TokenKind.FALSE,
            TokenKind.NULL,
        ):
            value = self._parse_value()
            mem = self.peek()
            if mem.kind is TokenKind.IN:
                self.advance()
                ident = self.expect(TokenKind.IDENT)
                return In(value, str(ident.value), negated=False)
            if mem.kind is TokenKind.NOT_IN:
                self.advance()
                ident = self.expect(TokenKind.IDENT)
                return In(value, str(ident.value), negated=True)
            raise FilterError("expected 'in' / 'not in' after value", mem.column)
        raise FilterError(f"unexpected token {first.value!r}", first.column)

    def _parse_value_list(self) -> tuple[object, ...]:
        """``[ value, value, ... ]`` — a trailing comma and an empty list are
        rejected, so a typo can't silently become a filter that matches nothing."""
        open_tok = self.expect(TokenKind.LBRACKET)
        values: list[object] = []
        while True:
            values.append(self._parse_value())
            nxt = self.peek()
            if nxt.kind is TokenKind.COMMA:
                self.advance()
                continue
            break
        end = self.peek()
        if end.kind is not TokenKind.RBRACKET:
            raise FilterError("expected ',' or ']' in list", end.column)
        self.advance()
        if not values:
            raise FilterError("empty list", open_tok.column)
        return tuple(values)

    def _parse_value(self) -> object:
        t = self.advance()
        if t.kind is TokenKind.STRING:
            return t.value
        if t.kind is TokenKind.NUMBER:
            return t.value
        if t.kind is TokenKind.DATE:
            return t.value
        if t.kind is TokenKind.TRUE:
            return True
        if t.kind is TokenKind.FALSE:
            return False
        if t.kind is TokenKind.NULL:
            return None
        if t.kind is TokenKind.IDENT:
            # Bare identifier on the value side is an error — values must be
            # quoted strings, numbers, dates, or keywords.
            raise FilterError(
                f"expected value, got identifier {t.value!r}; quote string values",
                t.column,
            )
        raise FilterError(f"expected value, got {t.value!r}", t.column)


# ── Evaluator ─────────────────────────────────────────────────────


Predicate = Callable[[Mapping[str, object]], bool]


def referenced_fields(node: object) -> frozenset[str]:
    """Every field name an AST references. Lets a caller decide policy for a
    field before evaluating, which the strict-null evaluator cannot express."""
    if isinstance(node, Compare):
        return frozenset({node.field})
    if isinstance(node, (In, FieldIn)):
        return frozenset({node.field})
    if isinstance(node, Not):
        return referenced_fields(node.operand)
    if isinstance(node, (And, Or)):
        return referenced_fields(node.left) | referenced_fields(node.right)
    return frozenset()


def compile_filter(text: str) -> Predicate:
    """Parse ``text`` into a callable predicate. Raises FilterError on
    syntax issues. The returned predicate is pure: it never raises and
    returns False on type mismatches or missing fields (strict null)."""
    tree = parse(text)
    return _make_evaluator(tree)


def parse_or_error(text: str) -> tuple[Predicate | None, FilterError | None]:
    """Same as :func:`compile_filter` but never raises — returns either a
    usable predicate or a structured error. Used by the TUI form so the
    user gets inline syntax feedback as they type."""
    if not text.strip():
        return None, None  # empty filter is valid: no predicate, no error
    try:
        return compile_filter(text), None
    except FilterError as e:
        return None, e


def _make_evaluator(node: object) -> Predicate:
    if isinstance(node, And):
        left = _make_evaluator(node.left)
        right = _make_evaluator(node.right)
        return lambda fm: left(fm) and right(fm)
    if isinstance(node, Or):
        left = _make_evaluator(node.left)
        right = _make_evaluator(node.right)
        return lambda fm: left(fm) or right(fm)
    if isinstance(node, Not):
        inner = _make_evaluator(node.operand)
        return lambda fm: not inner(fm)
    if isinstance(node, Compare):
        field, op, value = node.field, node.op, node.value
        return lambda fm: _eval_compare(fm, field, op, value)
    if isinstance(node, In):
        return lambda fm: _eval_in(fm, node.value, node.field, node.negated)
    if isinstance(node, FieldIn):
        return lambda fm: _eval_field_in(fm, node.field, node.values, node.negated)
    raise AssertionError(f"unknown AST node {type(node).__name__}")


def _eval_compare(fm: Mapping[str, object], field: str, op: str, value: object) -> bool:
    if field not in fm:
        # Strict null: missing field is False for every comparison.
        return False
    actual = fm[field]
    if op in ("==", "!="):
        equal = _scalar_equal(actual, value)
        return equal if op == "==" else not equal
    if op == "~~":
        if not isinstance(actual, str) or not isinstance(value, str):
            return False
        return fnmatch.fnmatchcase(actual, value)
    # Ordered compares: numeric-numeric or date-date only.
    if op in ("<", ">", "<=", ">="):
        if not _orderable(actual, value):
            return False
        if op == "<":
            return actual < value  # type: ignore[operator]
        if op == ">":
            return actual > value  # type: ignore[operator]
        if op == "<=":
            return actual <= value  # type: ignore[operator]
        if op == ">=":
            return actual >= value  # type: ignore[operator]
    return False


def _scalar_equal(actual: object, value: object) -> bool:
    """Equality that refuses bool/int conflation.

    ``True == 1`` in raw Python, but YAML ``true`` and ``1`` are distinct
    values, so exactly one side being a bool means not-equal.
    """
    if isinstance(actual, bool) != isinstance(value, bool):
        return False
    return bool(actual == value)


def _eval_field_in(
    fm: Mapping[str, object], field: str, values: tuple[object, ...], negated: bool
) -> bool:
    if field not in fm:
        return False  # strict null, as for every other comparison
    actual = fm[field]
    if isinstance(actual, list | tuple):
        return False  # a list field belongs on the ``In`` form
    member = any(_scalar_equal(actual, v) for v in values)
    return (not member) if negated else member


def _eval_in(fm: Mapping[str, object], value: object, field: str, negated: bool) -> bool:
    if field not in fm:
        return False  # strict null even for `not in`
    container = fm[field]
    if not isinstance(container, list | tuple | set | frozenset):
        # Sets included: every tag API returns frozenset, and a type gate that
        # rejected them would make ``'x' in file.tags.os`` silently False.
        return False
    is_member = value in container
    return (not is_member) if negated else is_member


def _orderable(a: object, b: object) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return False  # bool is a subtype of int — explicitly reject
    if isinstance(a, int | float) and isinstance(b, int | float):
        return True
    return isinstance(a, dt.date) and isinstance(b, dt.date)

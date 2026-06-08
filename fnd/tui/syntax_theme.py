"""Fixed-palette syntax highlighting for fenced code blocks.

Textual's stock fence highlighter (``textual.highlight.highlight``) lexes
with Pygments then maps tokens through a sparse ``HighlightTheme`` that
collapses most token types onto a handful of theme variables — flat-looking
output. :func:`highlight_fenced` keeps the same single Pygments pass but:

* maps through :class:`FNDSyntaxTheme`, a granular fixed-hex palette;
* differentiates bare ``Name`` tokens by position — Pygments has no semantic
  analysis, so in C/C++/Java a namespace, type, and variable all arrive as a
  bare ``Name`` (a sea of blue). Cheap positional heuristics recover the
  distinction: ``name(`` is a call, ``name::`` a scope, ``::name`` a qualified
  type, ``PascalCase`` a type, ``ALL_CAPS`` a constant;
* colours brackets ``()[]{}`` by nesting depth (rainbow brackets) so matching
  pairs are obvious. Angle brackets are skipped — Pygments tags them as
  ambiguous operators.

Colours are fixed truecolor, not ANSI: identical and legible for every user
against the dark tokyo-night chrome, independent of terminal.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from pygments.lexers import get_lexer_by_name
from pygments.token import Token
from pygments.util import ClassNotFound
from textual.content import Content, Span
from textual.highlight import HighlightTheme, guess_language

# Palette — shared by the theme map and the positional heuristics so a colour
# is defined once. Seeded from the user's terminal/VSCode colours.
_COMMENT = "#4E6B6E"
_STRING = "#CACACA"
_FUNCTION = "#FD8A38"
_VARIABLE = "#7BB7E2"
_KEYWORD = "#CC76D1"
_OPERATOR = "#DD5555"
_NUMBER = "#E0AF68"
_TYPE = "#5ADECD"  # classes, types, qualified members
_NAMESPACE = "#79E6F3"  # scope before `::`, and language builtins
_CONSTANT = "#F37F97"
_NEUTRAL = "#A9B1D6"  # separator punctuation (`;` `,` `.`)
_ERROR = "#FF4971"

# Rainbow-bracket depth cycle (the user's bright terminal hues — vivid enough
# to track nesting, distinct from the token colours above).
_BRACKET_CYCLE = ("#F2A272", "#B043D1", "#3FDCEE")
_OPENERS = frozenset("([{")
_CLOSERS = frozenset(")]}")


class FNDSyntaxTheme(HighlightTheme):
    """Granular token -> style map. Parents are specified so the parent-walk
    fallback in :func:`highlight_fenced` colours unlisted subtypes."""

    STYLES: dict[Any, str] = {  # noqa: RUF012  # matches base instance-var declaration
        Token.Comment: _COMMENT,  # no italic: matches the user's stripped comment fontStyle
        Token.Comment.Preproc: _KEYWORD,
        Token.Keyword: _KEYWORD,  # if/for/return/def/class
        Token.Keyword.Constant: _CONSTANT,  # True/False/None/nullptr
        Token.Keyword.Namespace: _KEYWORD,  # import/from
        Token.Keyword.Type: _KEYWORD,  # void/double/int — built-in type keywords read as keywords; teal is reserved for named types
        Token.Operator: _OPERATOR,
        Token.Operator.Word: _OPERATOR,  # and/or/not/in/is
        Token.Name: _VARIABLE,  # bare identifiers; refined by _name_style
        Token.Name.Variable: _VARIABLE,
        Token.Name.Function: _FUNCTION,
        Token.Name.Function.Magic: _FUNCTION,
        Token.Name.Decorator: _VARIABLE,
        Token.Name.Class: f"{_TYPE} bold",
        Token.Name.Namespace: _VARIABLE,
        Token.Name.Builtin: _NAMESPACE,  # print/len
        Token.Name.Builtin.Pseudo: f"{_CONSTANT} italic",  # self/cls
        Token.Name.Constant: _CONSTANT,
        Token.Name.Exception: _TYPE,
        Token.Name.Tag: _KEYWORD,
        Token.Name.Attribute: _FUNCTION,
        Token.Literal.String: _STRING,
        Token.Literal.String.Doc: _COMMENT,  # docstrings read as comments
        Token.Literal.String.Escape: _NUMBER,  # \n \t ...
        Token.Literal.String.Affix: _KEYWORD,  # f/r/b prefixes
        Token.Literal.String.Interpol: _FUNCTION,  # f-string {expr}
        Token.Literal.Number: _NUMBER,
        Token.Punctuation: _NEUTRAL,  # brackets overridden per-depth below
        Token.Generic.Inserted: _TYPE,  # diff +
        Token.Generic.Deleted: _OPERATOR,  # diff -
        Token.Generic.Heading: f"{_VARIABLE} bold",
        Token.Generic.Subheading: _VARIABLE,
        Token.Generic.Emph: "italic",
        Token.Generic.Strong: "bold",
        Token.Generic.Error: _ERROR,
        Token.Error: _ERROR,
        Token.Whitespace: "",
    }


# A non-whitespace token as (token_type, text).
_Tok = tuple[Any, str]


def _is_colon(tok: _Tok | None) -> bool:
    return tok is not None and tok[0] is Token.Operator and tok[1] == ":"


def _name_style(before: Sequence[_Tok], after: Sequence[_Tok], text: str) -> str:
    """Colour a bare ``Name`` by its surroundings (Pygments can't tell a
    namespace from a type from a variable). ``before``/``after`` are the
    nearest non-whitespace neighbours, nearest-first."""
    nxt = after[0] if after else None
    # `name(` — a call.
    if nxt is not None and nxt[0] is Token.Punctuation and nxt[1] == "(":
        return _FUNCTION
    # `name::` — a namespace / scope qualifier (`::` is two `:` operators).
    if _is_colon(nxt) and len(after) > 1 and _is_colon(after[1]):
        return _NAMESPACE
    # `::name` — a qualified type or member.
    if before and _is_colon(before[0]) and len(before) > 1 and _is_colon(before[1]):
        return _TYPE
    if len(text) >= 2 and text.isupper():  # ALL_CAPS — a constant / macro.
        return _CONSTANT
    if text[:1].isupper():  # PascalCase — a type / class.
        return _TYPE
    return _VARIABLE


def highlight_fenced(code: str, language: str | None) -> Content:
    """Syntax-highlight ``code`` with the FND palette.

    Same lexer setup and ``stylize_before`` tail as
    ``textual.highlight.highlight``, but maps through :class:`FNDSyntaxTheme`,
    refines bare ``Name`` tokens positionally, and rainbow-colours brackets."""
    language = language or guess_language(code, None)
    code = "\n".join(code.splitlines())
    try:
        lexer = get_lexer_by_name(language, stripnl=False, ensurenl=True, tabsize=8)
    except ClassNotFound:
        lexer = get_lexer_by_name("text", stripnl=False, ensurenl=True, tabsize=8)

    spans = _build_spans(list(lexer.get_tokens(code)))
    return Content(code, spans=spans).stylize_before("$text")


def _build_spans(tokens: Sequence[_Tok], *, neutral_operators: bool = False) -> list[Span]:
    """Map a token stream to coloured spans: bracket depth -> rainbow,
    bare ``Name`` -> positional heuristic, everything else -> parent-walk.
    Shared by the Pygments (fence) and regex (inline) tokenizers.

    ``neutral_operators`` renders operator/separator chars in the neutral
    foreground instead of red — for inline code, where ``/`` ``-`` ``:`` are
    usually path/flag separators in prose, not arithmetic. The ``::`` scope
    heuristic is unaffected (it keys on token type, not colour)."""
    # Compact view (whitespace dropped) for neighbour lookups, mapping each
    # token's index back to its position in ``compact``.
    compact = [(i, tt, tx) for i, (tt, tx) in enumerate(tokens) if tx.strip()]
    compact_pos = {i: p for p, (i, _, _) in enumerate(compact)}
    styles = FNDSyntaxTheme.STYLES

    spans: list[Span] = []
    start = 0
    depth = 0
    for index, (token_type, text) in enumerate(tokens):
        end = start + len(text)
        style: str | None = None

        if token_type is Token.Punctuation and text in _OPENERS:
            style = _BRACKET_CYCLE[depth % len(_BRACKET_CYCLE)]
            depth += 1
        elif token_type is Token.Punctuation and text in _CLOSERS:
            depth = max(0, depth - 1)
            style = _BRACKET_CYCLE[depth % len(_BRACKET_CYCLE)]
        elif token_type is Token.Name:
            p = compact_pos[index]
            before = [(compact[q][1], compact[q][2]) for q in range(p - 1, max(p - 3, -1), -1)]
            after = [(compact[q][1], compact[q][2]) for q in range(p + 1, min(p + 3, len(compact)))]
            style = _name_style(before, after, text)

        if style is None:  # walk up to the nearest themed parent token.
            walk = token_type
            while True:
                if mapped := styles.get(walk):
                    style = mapped
                    break
                if (walk := walk.parent) is None:
                    break
        if neutral_operators and style == _OPERATOR:
            style = _NEUTRAL
        if style:
            spans.append(Span(start, end, style))
        start = end
    return spans


# Lightweight tokenizer for inline code (`` `x` ``), which carries no language
# so a Pygments lexer can't be chosen. Emits token types compatible with
# :func:`_build_spans` so calls/scope/types/brackets colour as in fences;
# keywords are necessarily absent (no language to recognise them).
_INLINE_RE = re.compile(
    r"""
      (?P<name>[A-Za-z_]\w*)
    | (?P<number>\d[\w.]*)
    | (?P<string>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')
    | (?P<ws>\s+)
    | (?P<other>.)
    """,
    re.VERBOSE,
)
_OPERATOR_CHARS = frozenset("+-*/=<>!&|%^~@?:")


def _tokenize_inline(code: str) -> list[_Tok]:
    out: list[_Tok] = []
    for match in _INLINE_RE.finditer(code):
        kind = match.lastgroup
        text = match.group()
        if kind == "name":
            out.append((Token.Name, text))
        elif kind == "number":
            out.append((Token.Literal.Number, text))
        elif kind == "string":
            out.append((Token.Literal.String, text))
        elif kind == "ws":
            out.append((Token.Whitespace, text))
        elif text in _OPENERS or text in _CLOSERS or text in ".,;":
            out.append((Token.Punctuation, text))
        elif text in _OPERATOR_CHARS:
            out.append((Token.Operator, text))
        else:
            out.append((Token.Punctuation, text))
    return out


def inline_code_spans(code: str, offset: int = 0) -> list[Span]:
    """Coloured spans for a run of inline code, shifted by ``offset`` so they
    drop straight onto the parent block's content."""
    spans = _build_spans(_tokenize_inline(code), neutral_operators=True)
    if offset:
        spans = [Span(s.start + offset, s.end + offset, s.style) for s in spans]
    return spans

"""Obsidian / GitHub callout detection as a markdown-it token pass.

A callout is a blockquote whose first paragraph opens with ``[!type]``. The
pass retags the blockquote and splits that paragraph into a title paragraph and
a body paragraph, so the title gets its own line and its own CSS class rather
than being run into the body by markdown-it's softbreak.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from markdown_it.token import Token

__all__ = ["CALLOUT_TYPES", "CalloutStyle", "resolve_callout", "rewrite_callouts"]

_MARKER = re.compile(r"^\[!([A-Za-z][\w-]*)\]([-+]?)[ \t]*(.*)$")


@dataclass(frozen=True, slots=True)
class CalloutStyle:
    """Resolved presentation for one callout type."""

    key: str  # CSS class suffix
    icon: str  # single-cell glyph
    label: str  # title text when the callout gives none


def _style(key: str, icon: str) -> CalloutStyle:
    return CalloutStyle(key, icon, key.capitalize())


CALLOUT_TYPES: dict[str, CalloutStyle] = {
    "note": _style("note", "●"),
    "info": _style("info", "◉"),
    "todo": _style("todo", "○"),
    "abstract": _style("abstract", "○"),
    "summary": CalloutStyle("abstract", "○", "Summary"),
    "tldr": CalloutStyle("abstract", "○", "TLDR"),
    "tip": _style("tip", "◆"),
    "hint": CalloutStyle("tip", "◆", "Hint"),
    "important": CalloutStyle("tip", "◆", "Important"),
    "success": _style("success", "✔"),
    "check": CalloutStyle("success", "✔", "Check"),
    "done": CalloutStyle("success", "✔", "Done"),
    "question": _style("question", "?"),
    "help": CalloutStyle("question", "?", "Help"),
    "faq": CalloutStyle("question", "?", "FAQ"),
    "warning": _style("warning", "▲"),
    "caution": CalloutStyle("warning", "▲", "Caution"),
    "attention": CalloutStyle("warning", "▲", "Attention"),
    "failure": _style("failure", "✗"),
    "fail": CalloutStyle("failure", "✗", "Fail"),
    "missing": CalloutStyle("failure", "✗", "Missing"),
    "danger": _style("danger", "✖"),
    "error": CalloutStyle("danger", "✖", "Error"),
    "bug": _style("bug", "✖"),
    "example": _style("example", "▪"),
    "quote": _style("quote", '"'),
    "cite": CalloutStyle("quote", '"', "Cite"),
}


def resolve_callout(kind: str) -> CalloutStyle:
    """Style for ``kind``; an unknown type keeps its word under note styling."""
    known = CALLOUT_TYPES.get(kind.lower())
    if known is not None:
        return known
    return CalloutStyle("note", "●", kind)


def _split_children(children: list[Token]) -> tuple[list[Token], list[Token]]:
    """Children before / after the first line break of either kind."""
    for i, child in enumerate(children):
        if child.type in {"softbreak", "hardbreak"}:
            return children[:i], children[i + 1 :]
    return children, []


def _inline_token(children: list[Token], template: Token) -> Token:
    token = Token("inline", "", 0)
    token.children = children
    token.content = "".join(c.content for c in children)
    token.level = template.level
    token.map = template.map
    return token


def _paragraph_pair(template: Token) -> tuple[Token, Token]:
    opening = Token("paragraph_open", "p", 1)
    closing = Token("paragraph_close", "p", -1)
    for token in (opening, closing):
        token.level = template.level
        token.map = template.map
    return opening, closing


def rewrite_callouts(tokens: list[Token]) -> None:
    """Retag callout blockquotes and split their first paragraph, in place."""
    i = 0
    while i < len(tokens):
        if tokens[i].type != "blockquote_open" or i + 3 >= len(tokens):
            i += 1
            continue
        para_open, inline = tokens[i + 1], tokens[i + 2]
        if para_open.type != "paragraph_open" or inline.type != "inline":
            i += 1
            continue
        first_line = inline.content.partition("\n")[0]
        marker = _MARKER.match(first_line)
        if marker is None:
            i += 1
            continue
        kind, fold = marker.group(1), marker.group(2)
        style = resolve_callout(kind)
        tokens[i].meta["fnd_callout"] = style.key

        children = list(inline.children or [])
        head, tail = _split_children(children)
        if head:
            # `[` only opens a link before `](` or `[`, so the marker never
            # splits: it is always a prefix of the first text child.
            head[0].content = _MARKER.sub(r"\3", head[0].content, count=1).lstrip()
            if not head[0].content and len(head) == 1:
                head[0].content = style.label

        title_open, title_close = _paragraph_pair(para_open)
        title_open.meta["fnd_callout_title"] = (style.key, style.icon, bool(fold))
        replacement = [title_open, _inline_token(head, inline), title_close]
        if tail:
            body_open, body_close = _paragraph_pair(para_open)
            replacement += [body_open, _inline_token(tail, inline), body_close]
        tokens[i + 1 : i + 4] = replacement
        i += 1 + len(replacement)

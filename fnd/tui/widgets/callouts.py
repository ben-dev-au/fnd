"""Obsidian / GitHub callout detection as a markdown-it token pass.

A callout is a blockquote whose first paragraph opens with ``[!type]``. The
pass retags the blockquote and splits that paragraph into a title paragraph and
a body paragraph, so the title gets its own line and its own CSS class rather
than being run into the body by markdown-it's softbreak. A first pass tags the
containers and fence a chunk boundary cuts through, so the preview draws them whole.
"""

from __future__ import annotations

from dataclasses import dataclass

from markdown_it.token import Token

from fnd.callout_syntax import CALLOUT_MARKER, callout_marker
from fnd.seams import Seam

__all__ = [
    "CALLOUT_TYPES",
    "CalloutStyle",
    "resolve_callout",
    "rewrite_callouts",
    "stitch_seams",
]


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
        # A quote continued from above has its title in the chunk above.
        marker = (
            None if "above" in tokens[i].meta.get("fnd_seam", ()) else callout_marker(tokens, i)
        )
        if marker is None:
            i += 1
            continue
        para_open, inline = tokens[i + 1], tokens[i + 2]
        kind, fold = marker.group(1), marker.group(2)
        style = resolve_callout(kind)
        tokens[i].meta["fnd_callout"] = style.key

        children = list(inline.children or [])
        head, tail = _split_children(children)
        if head:
            # `[` only opens a link before `](` or `[`, so the marker never
            # splits: it is always a prefix of the first text child.
            head[0].content = CALLOUT_MARKER.sub(r"\3", head[0].content, count=1).lstrip()
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


_OPENERS = {
    "quote": {"blockquote_open"},
    "list": {"bullet_list_open", "ordered_list_open"},
    "item": {"list_item_open"},
}


def stitch_seams(tokens: list[Token], above: Seam | None, below: Seam | None) -> None:
    """Tag ``fnd_seam`` on each container and fence a chunk boundary cuts, in place."""
    if above is None and below is None:
        return
    children: dict[int | None, list[int]] = {}
    parents: list[int | None] = [None]
    for i, tok in enumerate(tokens):
        if tok.nesting >= 0:
            children.setdefault(parents[-1], []).append(i)
        if tok.nesting == 1:
            parents.append(i)
        elif tok.nesting == -1:
            parents.pop()
    for seam, side, pick in ((above, "above", 0), (below, "below", -1)):
        if seam is None:
            continue
        node: int | None = None
        for level in seam.levels:
            kind, _, detail = level.partition("=")
            kids = children.get(node, [])
            if not kids or tokens[kids[pick]].type not in _OPENERS.get(kind, ()):
                break
            node = kids[pick]
            tokens[node].meta.setdefault("fnd_seam", set()).add(side)
            if detail and kind == "quote":
                tokens[node].meta["fnd_callout"] = resolve_callout(detail).key
            elif detail.isdigit():
                tokens[node].meta["fnd_list_last"] = int(detail)
        else:
            kids = children.get(node, [])
            if seam.code and kids and tokens[kids[pick]].type in {"fence", "code_block"}:
                tokens[kids[pick]].meta.setdefault("fnd_seam", set()).add(side)

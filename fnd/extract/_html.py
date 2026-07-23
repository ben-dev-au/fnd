"""XHTML/HTML → structure walker, shared by the epub and web extractors.

Parses with ``lxml.html`` (libxml2's forgiving HTML parser — it lowercases
tags and ignores XML namespaces, so EPUB XHTML and loose web HTML both come
through as clean lowercase tags), then walks the document in reading order and
feeds a :class:`~fnd.extract._sectioner.HeadingSectioner` so headings, lists,
tables, code and prose land in ``body_md`` + ``body_struct`` the same way the
docx path produces them.
"""

from __future__ import annotations

import re

import lxml.html
from lxml.html import HtmlElement

from fnd.extract._sectioner import HeadingSectioner
from fnd.extract._tables import gfm_table

_WS = re.compile(r"\s+")
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
# Elements whose subtree carries no readable content.
_SKIP = {"script", "style", "head", "title", "meta", "link", "noscript", "svg", "template"}
# Structural containers we descend into without emitting anything ourselves.
_CONTAINERS = {
    "div",
    "section",
    "article",
    "main",
    "body",
    "header",
    "footer",
    "aside",
    "figure",
    "figcaption",
    "details",
    "summary",
    "html",
    "center",
    "span",
}
# Inline formatting → markdown wrapper.
_WRAP = {"strong": "**", "b": "**", "em": "*", "i": "*", "code": "`"}


def parse(content: bytes | str) -> HtmlElement:
    """Parse HTML/XHTML into an element tree (encoding auto-detected for bytes)."""
    return lxml.html.fromstring(content)


def _collapse(text: str) -> str:
    return _WS.sub(" ", text).strip()


def _inline_md(el: HtmlElement) -> str:
    """Render an inline subtree as markdown (bold/italic/code/links preserved)."""
    parts: list[str] = []
    if el.text:
        parts.append(el.text)
    for child in el:
        if not isinstance(child.tag, str):  # comment / PI
            if child.tail:
                parts.append(child.tail)
            continue
        tag = child.tag.lower()
        if tag == "br":
            parts.append(" ")
        elif tag in _SKIP:
            pass
        else:
            inner = _inline_md(child)
            wrap = _WRAP.get(tag)
            if wrap and inner.strip():
                parts.append(f"{wrap}{inner}{wrap}")
            else:
                parts.append(inner)
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def _list_items(el: HtmlElement, sec: HeadingSectioner, depth: int, ordered: bool) -> None:
    for li in el:
        if not isinstance(li.tag, str) or li.tag.lower() != "li":
            continue
        # The item's own text is its inline content minus any nested list.
        nested = [c for c in li if isinstance(c.tag, str) and c.tag.lower() in ("ul", "ol")]
        item_md_parts: list[str] = []
        if li.text:
            item_md_parts.append(li.text)
        for c in li:
            if not isinstance(c.tag, str):
                if c.tail:
                    item_md_parts.append(c.tail)
                continue
            if c.tag.lower() in ("ul", "ol"):
                if c.tail:
                    item_md_parts.append(c.tail)
                continue
            item_md_parts.append(_inline_md(c))
            if c.tail:
                item_md_parts.append(c.tail)
        item_md = _collapse("".join(item_md_parts))
        if item_md:
            sec.add_list_item(item_md, item_md, depth=depth, ordered=ordered)
        for sub in nested:
            _list_items(sub, sec, depth + 1, sub.tag.lower() == "ol")


def _table_md(el: HtmlElement) -> tuple[str, list[str]]:
    rows: list[list[str]] = []
    cells: list[str] = []
    for tr in el.iter("tr"):
        row: list[str] = []
        for cell in tr:
            if isinstance(cell.tag, str) and cell.tag.lower() in ("td", "th"):
                text = _collapse(cell.text_content())
                row.append(text)
                cells.append(text)
        if row:
            rows.append(row)
    return gfm_table(rows), cells


def _walk(el: HtmlElement, sec: HeadingSectioner) -> None:
    for child in el:
        if not isinstance(child.tag, str):
            continue
        tag = child.tag.lower()
        if tag in _SKIP:
            continue
        if tag in _HEADINGS:
            sec.add_heading(_HEADINGS[tag], _collapse(child.text_content()))
        elif tag == "p":
            sec.add_paragraph(_collapse(_inline_md(child)), _collapse(child.text_content()))
        elif tag in ("ul", "ol"):
            _list_items(child, sec, 0, tag == "ol")
        elif tag == "pre":
            sec.add_code(child.text_content())
        elif tag == "blockquote":
            sec.add_quote(_collapse(child.text_content()))
        elif tag == "table":
            md, cells = _table_md(child)
            sec.add_table(md, cells)
        elif tag in _CONTAINERS:
            _walk(child, sec)
        else:
            # Unknown block: descend if it has element children, else treat as prose.
            if len(child):
                _walk(child, sec)
            else:
                text = _collapse(child.text_content())
                if text:
                    sec.add_paragraph(text, text)


def walk_html(root: HtmlElement, sec: HeadingSectioner) -> None:
    """Feed the reading-order content of ``root`` into ``sec``."""
    body = root.body if root.tag == "html" and root.body is not None else root
    _walk(body, sec)

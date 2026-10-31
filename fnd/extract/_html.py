"""XHTML/HTML → structure walker, shared by the epub and web extractors.

Parses with ``lxml.html`` (libxml2's forgiving HTML parser: it lowercases
tags and ignores XML namespaces, so EPUB XHTML and loose web HTML both come
through as clean lowercase tags), then walks the document in reading order and
feeds a :class:`~fnd.extract._sectioner.HeadingSectioner` so headings, lists,
tables, code and prose land in ``body_md`` + ``body_struct`` the same way the
docx path produces them.
"""

from __future__ import annotations

import re
from itertools import chain
from typing import TYPE_CHECKING

import lxml.html
from lxml.etree import ParserError
from lxml.html import HtmlElement

from fnd.extract._sectioner import HeadingSectioner
from fnd.extract._tables import gfm_table

if TYPE_CHECKING:
    from fnd.extract._script_text import ScriptBlock

_WS = re.compile(r"\s+")
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
# Elements whose subtree carries no readable content.
_SKIP = {
    "script",
    "style",
    "head",
    "title",
    "meta",
    "link",
    "noscript",
    "svg",
    "template",
}
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
    """Parse HTML/XHTML into an element tree. Bytes that are valid UTF-8 are read
    as UTF-8: libxml2 assumes Latin-1 for a page that declares no charset."""
    if isinstance(content, bytes):
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            return lxml.html.fromstring(content)
        return lxml.html.fromstring(content, parser=lxml.html.HTMLParser(encoding="utf-8"))
    return lxml.html.fromstring(content)


def _collapse(text: str) -> str:
    return _WS.sub(" ", text).strip()


_BLOCK_TAGS = {
    *_HEADINGS,
    *("p", "ul", "ol", "li", "pre", "table", "blockquote", "div", "section", "article"),
    *("main", "header", "footer", "aside", "figure", "details", "form", "nav"),
}
_LANG_CLASS = re.compile(r"(?:^|\s)(?:language|lang)-([\w+#-]+)")
# Obsidian callout type for each class name a page might give an aside box.
_CALLOUT_KINDS = {
    "warn": "warning",
    "warning": "warning",
    "caution": "caution",
    "danger": "danger",
    "error": "danger",
    "note": "note",
    "info": "info",
    "tip": "tip",
    "hint": "tip",
    "important": "important",
    "success": "success",
}
# A whole class token (``warn``, ``alert-warning``), never a word inside one (``post-info``).
_CALLOUT_CLASS = re.compile(
    rf"(?:^|\s)(?:(?:alert|callout|admonition|notice|box)[-_])?({'|'.join(_CALLOUT_KINDS)})(?=\s|$)",
    re.I,
)
_BLOCK_ATTR = "fnd-block"  # set on an element with a block-level descendant
_INLINE_DATA_CHARS = 4_000
_BREAK = re.compile(r"<br\s*/?>", re.I)
_MD_LINK = re.compile(r"\[([^\[\]]*)\]\([^()]*\)")
_EMPHASIS_CLASS = re.compile(r"(?:^|\s)(?:key|takeaway|tldr)(?:\s|$)")
_BANNER_CLASS = re.compile(
    r"(?:^|\s)(?:stage|chapter|part|unit|phase)(?:[-_](?:h|head|header|heading|title|banner))?(?=\s|$)"
)
_SENTENCE_END = ".!?:;"
# Parents whose children are one run of text split by styling (syntax colouring, small caps).
_TIGHT = {
    "code",
    "pre",
    "kbd",
    "samp",
    "a",
    "em",
    "strong",
    "b",
    "i",
    "u",
    "p",
    "td",
    "th",
    "label",
    "small",
    "sub",
    "sup",
    "mark",
    *_HEADINGS,
}
_TAG = re.compile(r"</?[A-Za-z][^<>]*>")
_ENTITY = re.compile(r"&(?:#\d+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);")
# A list item's own text stops at the first of these; they render as blocks after it.
_ITEM_BLOCKS = {"pre", "details", "table", "blockquote", "div", "figure"}


def _needs_space(child: HtmlElement, last: str, glued: bool, loose: bool) -> bool:
    """Whether a page that only spaces ``child`` from the text before it with CSS needs a space.

    Minified markup leaves nothing between sibling widgets (``<a>Home</a><a>About</a>``).
    """
    if str(child.tag).lower() == "br" or not last:
        return False
    starts_sentence = last in _SENTENCE_END and child.text_content().lstrip()[:1].isupper()
    return (glued and loose and not last.isspace()) or starts_sentence


def _append_tail(parts: list[str], child: HtmlElement, bold_title: bool, last: str) -> None:
    """Add ``child``'s tail; ``last`` is the last character the child showed."""
    tail = child.tail
    if not tail:
        return
    ends_sentence = bool(last) and last in _SENTENCE_END
    if tail[:1].isalnum() and (ends_sentence or bold_title):
        parts.append(" ")
    parts.append(tail)


def _is_title(child: HtmlElement, seen: bool) -> bool:
    """A bold lead-in: the first words of the text, with nothing visible before it."""
    return str(child.tag).lower() in ("b", "strong") and not seen


def _is_prose(el: HtmlElement) -> bool:
    """Whether ``el`` has words of its own around its children (not a bare run of widgets)."""
    return bool((el.text or "").strip()) or any((c.tail or "").strip() for c in el)


def _is_loose(el: HtmlElement) -> bool:
    """Whether the children of ``el`` are separate items rather than pieces of one phrase."""
    return str(el.tag).lower() not in _TIGHT and not _is_prose(el)


def _render(el: HtmlElement, plain: bool, tight: bool = False) -> tuple[str, str]:
    """Inline subtree as (markdown, last visible character); ``plain`` drops the markup.

    ``tight``: an ancestor is one run of text, so nothing below it is a separate widget.
    """
    parts: list[str] = []
    last = ""
    if el.text:
        parts.append(el.text)
        last = el.text[-1]
    loose = not tight and _is_loose(el)
    glued, seen = False, bool((el.text or "").strip())
    for child in el:
        if not isinstance(child.tag, str):  # comment / PI
            if child.tail:
                parts.append(child.tail)
                last = child.tail[-1]
                seen = seen or bool(child.tail.strip())
            continue
        if _needs_space(child, last, glued, loose):
            parts.append(" ")
            last = " "
        glued = not child.tail
        text, piece_last = _piece(child, plain, not loose)
        parts.append(text)
        last = piece_last or last
        _append_tail(parts, child, _is_title(child, seen), piece_last)
        seen = seen or bool(text.strip()) or bool((child.tail or "").strip())
        if child.tail:
            last = child.tail[-1]
    return "".join(parts), last


def _piece(child: HtmlElement, plain: bool, tight: bool) -> tuple[str, str]:
    """One inline child as (text, last visible character)."""
    tag = str(child.tag).lower()
    if tag == "br":
        return " ", " "
    if tag in _SKIP:
        return "", ""
    inner, last = _render(child, plain, tight)
    href = child.get("href") if tag == "a" else None
    wrap = _WRAP.get(tag)
    if plain or not inner.strip():
        return inner, last
    if href:  # the preview keeps the link; the index text has only the words
        return f"[{inner}]({href})", last
    return (f"{wrap}{inner}{wrap}" if wrap else inner), last


def _inline_md(el: HtmlElement, plain: bool = False) -> str:
    return _render(el, plain)[0]


def _inline_text(el: HtmlElement) -> str:
    return _collapse(_inline_md(el, plain=True))


class _Run:
    """A paragraph of a list item built child by child: markdown and its plain-text twin."""

    def __init__(self, item: HtmlElement, lead: str = "") -> None:
        self.loose = _is_loose(item)
        self.md, self.txt = [lead], [lead]
        self.last, self.seen, self.glued = lead[-1:], bool(lead.strip()), False

    def text(self, text: str) -> None:
        self.md.append(text)
        self.txt.append(text)
        self.last = text[-1:] or self.last
        self.seen = self.seen or bool(text.strip())

    def add(self, child: HtmlElement) -> None:
        """Add one inline child and its tail."""
        if _needs_space(child, self.last, self.glued, self.loose):
            self.text(" ")
        self.glued = not child.tail
        md, last = _piece(child, False, not self.loose)
        txt = _piece(child, True, not self.loose)[0]
        title = _is_title(child, self.seen)
        self.md.append(md)
        self.txt.append(txt)
        self.last = last or self.last
        self.seen = self.seen or bool(txt.strip())
        _append_tail(self.md, child, title, last)
        _append_tail(self.txt, child, title, last)
        if child.tail:
            self.last = child.tail[-1]
            self.seen = self.seen or bool(child.tail.strip())

    def lines(self) -> tuple[str, str]:
        """(markdown, plain text), whitespace collapsed."""
        return _collapse("".join(self.md)), _collapse("".join(self.txt))

    def reset(self) -> None:
        self.md, self.txt = [], []
        self.last, self.seen, self.glued = "", False, False


def _list_items(
    el: HtmlElement,
    sec: HeadingSectioner,
    depth: int,
    ordered: bool,
    attach: dict[str, list[ScriptBlock]],
) -> None:
    number = 0
    for li in el:
        if not isinstance(li.tag, str) or li.tag.lower() != "li":
            continue
        number += 1
        # The item's own text is its inline content up to the first block child;
        # later blocks and nested lists follow it.
        run = _Run(li, li.text or "")
        rest: list[HtmlElement] = []  # from the first block or nested list on, in page order
        for c in li:
            tag = c.tag.lower() if isinstance(c.tag, str) else ""
            lone_p = tag == "p" and run.seen
            if rest or tag in ("ul", "ol") or tag in _ITEM_BLOCKS or lone_p:
                rest.append(c)
            elif not tag:
                run.text(c.tail or "")
            else:
                run.add(c)
        md, item = run.lines()
        bare = not item and rest and rest[0].tag in ("div", "p")
        if bare and _inline_only(rest[0]):  # an item whose text sits in a leading block
            head = rest.pop(0)
            md = _collapse(f"{_inline_md(head)} {head.tail or ''}")
            item = _collapse(f"{_inline_text(head)} {head.tail or ''}")
        if item:
            sec.add_list_item(md, item, depth=depth, ordered=ordered, number=number)
        _item_rest(li, rest, sec, depth, attach)


def _item_rest(
    li: HtmlElement,
    rest: list[HtmlElement],
    sec: HeadingSectioner,
    depth: int,
    attach: dict[str, list[ScriptBlock]],
) -> None:
    """What follows a list item's first block: blocks, nested lists, and the text between them."""
    run = _Run(li)
    for c in rest:
        tag = c.tag.lower() if isinstance(c.tag, str) else ""
        if tag and tag not in ("ul", "ol", "p") and tag not in _ITEM_BLOCKS:
            run.add(c)
            continue
        _flush(run, sec)
        if tag in ("ul", "ol"):
            _list_items(c, sec, depth + 1, tag == "ol", attach)
        else:
            _walk_child(c, sec, attach)
        run.text(c.tail or "")
    _flush(run, sec)


def _flush(run: _Run, sec: HeadingSectioner) -> None:
    md, text = run.lines()
    if text:
        sec.add_paragraph(md, text)
    run.reset()


def _table_md(el: HtmlElement) -> tuple[str, list[str]]:
    rows: list[list[str]] = []
    cells: list[str] = []
    for tr in el.iter("tr"):
        section = tr.getparent()  # a nested table's rows are rendered inside their cell instead
        if section is not el and (section is None or section.getparent() is not el):
            continue
        row: list[str] = []
        for cell in tr:
            if isinstance(cell.tag, str) and cell.tag.lower() in ("td", "th"):
                row.append(_collapse(_inline_md(cell)))
                cells.append(_inline_text(cell))
        if row:
            rows.append(row)
    return gfm_table(rows), cells


def _cell_md(cell: str) -> str:
    """A table cell's markdown: inline HTML converted, line breaks kept as ``/``."""
    if not _TAG.search(cell) and not _ENTITY.search(cell):
        return cell
    holder = _fragment(_BREAK.sub(" / ", cell))
    return _collapse(_inline_md(holder)) if holder is not None else _collapse(_TAG.sub("", cell))


def _fragment(markup: str) -> HtmlElement | None:
    """Parse a script-built HTML fragment; None when the parser rejects it."""
    try:
        holder = lxml.html.fragment_fromstring(markup, create_parent="div")
    except (ValueError, ParserError, AssertionError):  # AssertionError: "too many bodies"
        return None
    _mark_blocks(holder)
    return holder


def add_blocks(sec: HeadingSectioner, blocks: list[ScriptBlock], *, level: int = 0) -> None:
    """Emit script-derived blocks; paragraphs keep their inline markup as markdown.

    A data section's name opens a heading at ``level``; with no level it is a bold lead.
    """
    for block in blocks:
        try:
            _add_block(sec, block, level)
        except RecursionError:  # a fragment nested too deep for the stack the page already uses
            plain = _collapse(_TAG.sub("", block.text))
            if plain:
                sec.add_paragraph(plain, plain)


def _add_block(sec: HeadingSectioner, block: ScriptBlock, level: int) -> None:
    if block.kind == "h":
        if level:
            sec.add_heading(level, block.text)
        else:
            sec.add_paragraph(f"**{block.text}**", block.text)
    elif block.kind == "code":
        sec.add_code(block.text, block.lang)
    elif block.kind == "table":
        rows = [[_cell_md(c) for c in row] for row in block.rows]
        cells = [_MD_LINK.sub(r"\1", c).replace("`", "").replace("**", "") for r in rows for c in r]
        sec.add_table(gfm_table(rows), cells)
    elif block.kind == "html":
        holder = _fragment(block.text)
        if holder is None:
            sec.add_paragraph(
                _collapse(_TAG.sub("", block.text)), _collapse(_TAG.sub("", block.text))
            )
        elif _inline_only(holder):
            _paragraph(sec, holder)
        else:
            _walk(holder, sec, {})
    else:
        md, text = block.text, block.text
        markup = _TAG.search(block.text) or _ENTITY.search(block.text)
        if markup and (holder := _fragment(block.text)) is not None:
            md, text = _collapse(_inline_md(holder)), _inline_text(holder)
        if block.kind == "li":
            sec.add_list_item(md, text)
        elif text:
            sec.add_paragraph(md, text)


def _heading_text(el: HtmlElement) -> str:
    """Heading text with a leading number badge (``<span>5</span>Title``) spaced off."""
    full = _collapse(el.text_content())
    first = el[0] if len(el) and not el.text else None
    badge = (
        _collapse(first.text_content()) if first is not None and isinstance(first.tag, str) else ""
    )
    if badge.isdigit() and full.startswith(badge) and full[len(badge) :][:1] not in " .):":
        return f"{badge} {full[len(badge) :]}".strip()
    return full


def _code_lang(pre: HtmlElement) -> str:
    """Language named by a ``language-x`` / ``lang-x`` class on a ``<pre>`` or its ``<code>``."""
    for el in (pre, *pre.iter("code")):
        if m := _LANG_CLASS.search(el.get("class", "")):
            return m.group(1).lower()
    return ""


def _callout(el: HtmlElement) -> tuple[str, str, str, str] | None:
    """(kind, title, body markdown, body text) of a ``<div class="warn"><b>T</b>body</div>`` box."""
    m = _CALLOUT_CLASS.search(el.get("class", ""))
    if el.tag != "div" or not m or not _inline_only(el):
        return None
    title = ""
    if len(el) and el[0].tag in ("b", "strong") and not (el.text or "").strip():
        title = _collapse(el[0].text_content())
    md = _collapse(_inline_md(el))
    text = _inline_text(el)
    if title:
        md = md.removeprefix(f"**{title}**").strip()
        text = text.removeprefix(title).strip()
    kind = _CALLOUT_KINDS[m.group(1).lower()]
    return (kind, title, md, text) if md or title else None


def _banner(el: HtmlElement) -> tuple[str, str] | None:
    """(title, subtitle) of a stage/chapter banner: ``<div class="stage-h"><b>T</b><span>S</span>``."""
    if el.tag != "div" or not _BANNER_CLASS.search(el.get("class", "")):
        return None
    kids = [c for c in el if isinstance(c.tag, str)]
    if not kids or kids[0].tag not in ("b", "strong") or any(c.tag != "span" for c in kids[1:]):
        return None
    title = _collapse(kids[0].text_content())
    return (title, _collapse(" ".join(c.text_content() for c in kids[1:]))) if title else None


def _mark_blocks(root: HtmlElement) -> None:
    """Flag every element that has a block-level descendant, in one pass over the tree."""
    for el in reversed(list(root.iter())):  # reverse document order: children before parents
        parent = el.getparent()
        if parent is None or not isinstance(el.tag, str):
            continue
        if el.tag.lower() in _BLOCK_TAGS or el.get(_BLOCK_ATTR) is not None:
            parent.set(_BLOCK_ATTR, "1")


def _inline_only(el: HtmlElement) -> bool:
    """Whether ``el`` holds no block-level element (the tree went through ``_mark_blocks``)."""
    return el.get(_BLOCK_ATTR) is None


def _paragraph(sec: HeadingSectioner, el: HtmlElement, *, bold: bool = False) -> None:
    md, text = _collapse(_inline_md(el)), _inline_text(el)
    if md and text:
        sec.add_paragraph(f"**{text}**" if bold else md, text)


def _stray(sec: HeadingSectioner, text: str | None) -> None:
    """Text sitting directly between a container's block children."""
    if text and text.strip():
        sec.add_paragraph(_collapse(text), _collapse(text))


def _walk(el: HtmlElement, sec: HeadingSectioner, attach: dict[str, list[ScriptBlock]]) -> None:
    _stray(sec, el.text)
    for child in el:
        _walk_child(child, sec, attach)
        _stray(sec, child.tail)


def _walk_child(
    child: HtmlElement, sec: HeadingSectioner, attach: dict[str, list[ScriptBlock]]
) -> None:
    if not isinstance(child.tag, str):
        return
    tag = child.tag.lower()
    if tag in _SKIP:
        _attach(child, sec, attach)  # a JSON island's own data is placed where it sits
        return
    if tag in _HEADINGS:
        sec.add_heading(_HEADINGS[tag], _heading_text(child))
    elif tag == "p":
        _paragraph(sec, child, bold=bool(_EMPHASIS_CLASS.search(child.get("class", ""))))
    elif tag in ("ul", "ol"):
        _list_items(child, sec, 0, tag == "ol", attach)
    elif tag == "pre":
        sec.add_code(child.text_content(), _code_lang(child))
    elif tag == "blockquote":
        sec.add_quote(_inline_text(child), _collapse(_inline_md(child)))
    elif tag == "table":
        md, cells = _table_md(child)
        sec.add_table(md, cells)
    elif (callout := _callout(child)) is not None:
        sec.add_callout(*callout)
    elif (banner := _banner(child)) is not None:
        sec.add_heading(2, banner[0])
        if banner[1]:
            sec.add_paragraph(banner[1], banner[1])
    elif tag == "summary":
        _paragraph(sec, child, bold=True)
    elif (tag in _CONTAINERS or len(child)) and tag != "dl" and _inline_only(child):
        _paragraph(sec, child)
    elif tag in _CONTAINERS or len(child):
        _walk(child, sec, attach)
        _attach(child, sec, attach)
        return
    else:
        text = _collapse(child.text_content())
        if text:
            sec.add_paragraph(text, text)
    _attach(child, sec, attach)
    for island in child.iterdescendants("script"):  # flattened above, so never walked
        _attach(island, sec, attach)


def _attach(el: HtmlElement, sec: HeadingSectioner, attach: dict[str, list[ScriptBlock]]) -> None:
    """Emit the script blocks named by ``el``'s ``id`` or a ``data-*`` attribute."""
    for name, value in el.attrib.items():  # an empty value (data-v-1a2b="") names nothing
        if value and (name == "id" or name.startswith("data-")) and value in attach:
            blocks = attach.pop(value)
            size = sum(len(b.text) + sum(map(len, chain(*b.rows))) for b in blocks)
            # A widget's data reads as part of its section; a whole dataset gets sections.
            level = min(max(sec.depth + 1, 2), 6) if size > _INLINE_DATA_CHARS else 0
            add_blocks(sec, blocks, level=level)


def walk_html(
    root: HtmlElement, sec: HeadingSectioner, attach: dict[str, list[ScriptBlock]] | None = None
) -> None:
    """Feed the reading-order content of ``root`` into ``sec``.

    ``attach`` maps an attribute value to script blocks emitted after the element
    carrying it; entries not claimed by any element are left in the dict.
    """
    _mark_blocks(root)
    body = root.body if root.tag == "html" and root.body is not None else root
    _walk(body, sec, {} if attach is None else attach)

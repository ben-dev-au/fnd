"""Obsidian inline syntax, applied to a block's built Content.

Runs post-build rather than as markdown-it plugins: Textual's
``_token_to_content`` pushes nothing for an unknown ``x_open`` but pops for any
``*_close``, so a plugin emitting new inline pairs unbalances its style stack.
Editing the finished Content keeps Textual's internals unforked.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from fnd.matching import MatchSpec
from fnd.render import match_word_spans
from fnd.tui.widgets.content_edits import Edit, apply_edits

if TYPE_CHECKING:
    from textual.widgets._markdown import MarkdownBlock

__all__ = [
    "MARK_STYLE",
    "REVEAL_STYLE",
    "TAG_STYLE",
    "WIKILINK_STYLE",
    "apply_obsidian_inline",
    "collect_edits",
]

# Underline, not a background: a background would read as a search hit, which is
# the only thing in the preview that paints one.
MARK_STYLE = "underline #e0af68"
WIKILINK_STYLE = "#7dcfff"
TAG_STYLE = "#bb9af7"
REVEAL_STYLE = "#565f89"

# Cheap gate: most blocks contain none of these and skip every regex below.
_CANDIDATES = frozenset("=[!#%^")

_MARK = re.compile(r"==(?!\s)(.+?)(?<!\s)==")
_CHECKBOX = re.compile(r"^\[([ xX])\]\s")
_EMBED = re.compile(r"!\[\[([^\]|]+?)(?:\|([^\]]*))?\]\]")
_WIKILINK = re.compile(r"(?<!!)\[\[([^\]|]+?)(?:\|([^\]]*))?\]\]")
# ``#`` must start a word and be followed by a letter, so the heading marker
# prefix (``## ``) and ``C#`` are both excluded.
_TAG = re.compile(r"(?:(?<=\s)|^)#[A-Za-z][\w/-]*")
_COMMENT = re.compile(r"%%(.+?)%%", re.S)
_BLOCK_ID = re.compile(r"(?:(?<=\s)|^)\^[A-Za-z0-9-]+$")


def has_match(text: str, spec: MatchSpec) -> bool:
    """True when ``spec`` matches a word in ``text`` — the reveal test."""
    if spec.is_empty or not text:
        return False
    return bool(match_word_spans(text, spec))


def _protected(block: MarkdownBlock) -> set[int]:
    """Character positions covered by an inline-code span."""
    out: set[int] = set()
    for span in block._content.spans:
        if str(span.style) == ".code_inline":
            out.update(range(span.start, span.end))
    return out


def collect_edits(
    plain: str, *, protected: set[int], spec: MatchSpec, list_item: bool
) -> list[Edit]:
    """Non-overlapping edits that render the Obsidian syntax found in ``plain``."""
    edits: list[Edit] = []
    taken: set[int] = set()

    def free(start: int, end: int) -> bool:
        return not any(p in protected or p in taken for p in range(start, end))

    if list_item:
        box = _CHECKBOX.match(plain)
        if box is not None and free(box.start(), box.end()):
            glyph = "☐ " if box.group(1) == " " else "☑ "
            edits.append(Edit(box.start(), box.end(), glyph))
            taken.update(range(box.start(), box.end()))

    for m in _MARK.finditer(plain):
        if not free(m.start(), m.end()):
            continue
        inner = m.group(1)
        edits.append(Edit(m.start(), m.end(), inner, ((0, len(inner), MARK_STYLE),)))
        taken.update(range(m.start(), m.end()))

    for m in _EMBED.finditer(plain):
        if not free(m.start(), m.end()):
            continue
        name = (m.group(2) or m.group(1)).strip()
        shown = f"▣ {name}"
        edits.append(Edit(m.start(), m.end(), shown, ((0, len(shown), WIKILINK_STYLE),)))
        taken.update(range(m.start(), m.end()))

    for m in _WIKILINK.finditer(plain):
        if not free(m.start(), m.end()):
            continue
        target, alias = m.group(1).strip(), (m.group(2) or "").strip()
        styles: tuple[tuple[int, int, str], ...]
        if not alias:
            shown, styles = target, ((0, len(target), WIKILINK_STYLE),)
        elif has_match(target, spec) and not has_match(alias, spec):
            # The target reaches F_BODY, so a match in it must stay visible even
            # though Obsidian hides it behind the alias.
            shown = f"{alias} ⟨{target}⟩"
            styles = (
                (0, len(alias), WIKILINK_STYLE),
                (len(alias) + 1, len(target) + 2, REVEAL_STYLE),
            )
        else:
            shown, styles = alias, ((0, len(alias), WIKILINK_STYLE),)
        edits.append(Edit(m.start(), m.end(), shown, styles))
        taken.update(range(m.start(), m.end()))

    for m in _TAG.finditer(plain):
        if not free(m.start(), m.end()):
            continue
        text = m.group(0)
        edits.append(Edit(m.start(), m.end(), text, ((0, len(text), TAG_STYLE),)))
        taken.update(range(m.start(), m.end()))

    for m in _COMMENT.finditer(plain):
        if not free(m.start(), m.end()):
            continue
        inner = m.group(1)
        # Comments reach F_BODY, so one holding a match stays visible rather than
        # being hidden the way Obsidian hides it.
        if has_match(inner, spec):
            edits.append(Edit(m.start(), m.end(), inner, ((0, len(inner), REVEAL_STYLE),)))
        else:
            edits.append(Edit(m.start(), m.end(), ""))
        taken.update(range(m.start(), m.end()))

    for m in _BLOCK_ID.finditer(plain):
        if not free(m.start(), m.end()):
            continue
        if has_match(m.group(0).lstrip("^"), spec):
            continue
        edits.append(Edit(max(m.start() - 1, 0), m.end(), ""))
        taken.update(range(m.start(), m.end()))

    return edits


def apply_obsidian_inline(block: MarkdownBlock, spec: MatchSpec) -> None:
    """Rewrite ``block``'s content in place for Obsidian inline syntax."""
    from textual.widgets._markdown import (
        MarkdownOrderedListItem,
        MarkdownUnorderedListItem,
    )

    plain = block._content.plain
    if not plain or _CANDIDATES.isdisjoint(plain):
        return
    list_item = isinstance(block, MarkdownOrderedListItem | MarkdownUnorderedListItem)
    edits = collect_edits(plain, protected=_protected(block), spec=spec, list_item=list_item)
    if edits:
        block.set_content(apply_edits(block._content, edits))

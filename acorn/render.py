"""Render ``body_struct`` (list of :class:`Block`) to Markdown source for
Textual's :class:`textual.widgets.Markdown` widget, with query-term highlights
applied via a Markdown bold wrap.

Per plan §5: preview should show structured text (headings / paragraphs /
lists), not a raw blob. Phase 5 ships the simplest faithful renderer; phase 7
adds match-cluster minimap.
"""

from __future__ import annotations

import re

from acorn.extract.base import Block

_HEADING_KINDS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})


def _highlight(text: str, terms: list[str]) -> str:
    """Wrap each whole-word occurrence of any term in Markdown bold (**…**).

    Case-insensitive. Long-term match wins on ties so "supersymmetry" beats
    "super" when both are queried.
    """
    if not terms:
        return text
    # Escape regex metas in terms; sort longest-first.
    safe = sorted({re.escape(t) for t in terms if t}, key=len, reverse=True)
    if not safe:
        return text
    pattern = re.compile(r"\b(" + "|".join(safe) + r")\b", re.IGNORECASE)
    return pattern.sub(lambda m: f"**{m.group(0)}**", text)


def _terms_from_query(query: str) -> list[str]:
    """Pull plain-word terms out of a query string for highlighting.

    Strips operators (AND/OR/NOT/+/-/parens/quotes/wildcards/fuzzy/range
    syntax) and field qualifiers like ``kind:pdf`` so the highlighter only
    bolds genuine search terms."""
    if not query:
        return []
    # Drop bracketed range syntax.
    q = re.sub(r"\[[^\]]*\]", " ", query)
    # Drop {N} proximity prefix.
    q = re.sub(r"\{\d+\}", " ", q)
    # Drop NEAR/N marker.
    q = re.sub(r"\bNEAR/\d+\b", " ", q)
    # Drop field qualifiers: word followed by colon then non-space.
    q = re.sub(r"\b\w+:\S+", " ", q)
    # Drop Tantivy operators / sigils.
    q = re.sub(r"[+\-()\"~*?]", " ", q)
    # Drop bare AND / OR / NOT.
    q = re.sub(r"\b(AND|OR|NOT)\b", " ", q)
    return [w for w in q.split() if w]


def render(blocks: list[Block], *, query: str = "") -> str:
    """Return Markdown source for the preview pane."""
    terms = _terms_from_query(query)
    parts: list[str] = []
    for b in blocks:
        text = _highlight(b.text, terms)
        if b.kind in _HEADING_KINDS:
            level = int(b.kind[1])
            parts.append(f"{'#' * level} {text}\n")
        elif b.kind == "ul":
            parts.append(f"- {text}\n")
        elif b.kind == "ol":
            parts.append(f"1. {text}\n")
        elif b.kind == "code":
            parts.append(f"```\n{b.text}\n```\n")  # don't bold inside code
        elif b.kind == "quote":
            for line in text.splitlines() or [text]:
                parts.append(f"> {line}\n")
        else:
            parts.append(f"{text}\n\n")
    return "".join(parts).strip() + "\n"

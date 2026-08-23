"""Text edits over a Textual ``Content``, with span remapping."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from textual.content import Content, Span

__all__ = ["Edit", "apply_edits"]


@dataclass(frozen=True, slots=True)
class Edit:
    """Replace ``plain[start:end]`` with ``replacement``.

    ``styles`` are ``(offset, length, style)`` triples relative to
    ``replacement``.
    """

    start: int
    end: int
    replacement: str
    styles: tuple[tuple[int, int, str], ...] = ()


def apply_edits(content: Content, edits: Sequence[Edit]) -> Content:
    """Apply ``edits`` to ``content``, carrying its existing spans across.

    A span wholly inside an edit is dropped, a partial overlap excludes
    the replacement, and a span that wholly contains an edit still spans
    it. Edits must not overlap.
    """
    if not edits:
        return content
    ordered = sorted(edits, key=lambda e: e.start)
    plain = content.plain
    pieces: list[str] = []
    spans: list[Span] = []
    # (old_start, old_end, new_start, new_len) per edit, in order.
    table: list[tuple[int, int, int, int]] = []
    cursor = 0
    delta = 0
    for edit in ordered:
        if edit.start < cursor:
            raise ValueError(
                f"edit ({edit.start}, {edit.end}) overlaps a preceding edit ending at {cursor}"
            )
        pieces.append(plain[cursor : edit.start])
        new_start = edit.start + delta
        pieces.append(edit.replacement)
        for offset, length, style in edit.styles:
            spans.append(Span(new_start + offset, new_start + offset + length, style))
        table.append((edit.start, edit.end, new_start, len(edit.replacement)))
        delta += len(edit.replacement) - (edit.end - edit.start)
        cursor = edit.end
    pieces.append(plain[cursor:])

    def remap(pos: int, *, is_end: bool) -> int:
        """Map a span boundary, excluding replaced text a span did not wholly contain."""
        shift = 0
        for old_start, old_end, new_start, new_len in table:
            if pos < old_start:
                break
            if old_start < pos < old_end or pos == old_start == old_end:
                return new_start if is_end else new_start + new_len
            if pos == old_start:
                return new_start
            shift += new_len - (old_end - old_start)
        return pos + shift

    for span in content.spans:
        start, end = remap(span.start, is_end=False), remap(span.end, is_end=True)
        if end > start:
            spans.append(Span(start, end, span.style))
    return Content("".join(pieces), spans=spans)

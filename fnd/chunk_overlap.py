"""Window overlap between consecutive chunks of one file.

The txt, code and data extractors cut overlapping windows, so a phrase across a
boundary is still indexed whole. The preview shows each repeated stretch once:
:func:`mark_repeats` sets each chunk's ``repeat`` to the characters at the end of
its text that the next chunk repeats, verified against both texts, and trims a
fenced window's ``body_md`` to match, seamed so the two halves read as one
listing. ``body_text`` stays whole, so the flat builder can still paint a word
cut by the boundary.

The repeat lives at the END of the earlier chunk, so the later chunk keeps its
head: a chunk whose matches all sit in its repeat lands on the next chunk,
whose first match is that same one. A trimmed fence carries the index windows
that cover what it shows, so a phrase or proximity pair the cut runs through
is highlighted as either window sees it.
"""

from __future__ import annotations

import dataclasses
from itertools import dropwhile, pairwise
from typing import TYPE_CHECKING

from fnd.extract._fences import fenced, unfenced
from fnd.extract.plain import OVERLAP_CHARS
from fnd.kinds import KIND_BY_ID
from fnd.seams import Seam, with_seams

if TYPE_CHECKING:
    from fnd.query import FileChunk

__all__ = ["mark_repeats", "shown_text"]


def mark_repeats(chunks: list[FileChunk]) -> list[FileChunk]:
    """``chunks`` (in ``chunk_seq`` order) with every verified window overlap marked."""
    repeats = [_repeated(a, b) for a, b in pairwise(chunks)] + [0]
    out: list[FileChunk] = []
    lead = ""
    for i, chunk in enumerate(chunks):
        before = chunks[i - 1] if i > 0 and repeats[i - 1] else None
        if repeats[i] or before is not None:
            chunk, lead = _trimmed(
                chunk, repeats[i], before=before, before_repeat=repeats[i - 1], lead=lead
            )
        else:
            lead = ""
        out.append(chunk)
    return out


def shown_text(chunk: FileChunk, repeat: int | None = None) -> str:
    """``chunk``'s text less the stretch the next chunk shows."""
    text = chunk.body_text
    return text[: len(text) - (chunk.repeat if repeat is None else repeat)]


def _repeated(prev: FileChunk, curr: FileChunk) -> int:
    """Characters at the end of ``prev`` that ``curr`` repeats, or 0. A blank txt
    head proves nothing: a skipped blank window leaves matching blank ends."""
    if prev.kind != curr.kind:
        return 0
    a, b = prev.body_text, curr.body_text
    if prev.kind == "txt":
        head = b[:OVERLAP_CHARS]
        cut = len(a) - OVERLAP_CHARS
        adjacent = head.strip() != "" and curr.line == prev.line + a.count("\n", 0, cut)
        return (
            OVERLAP_CHARS
            if len(head) == OVERLAP_CHARS < len(a) and adjacent and a.endswith(head)
            else 0
        )
    if not (_is_line_window(prev) and _is_line_window(curr)):
        return 0
    a_lines = a.split("\n")
    k = prev.line + len(a_lines) - curr.line
    b_lines = b.split("\n")
    if not 0 < k < len(a_lines) or k > len(b_lines) or a_lines[-k:] != b_lines[:k]:
        return 0
    return len("\n".join(b_lines[:k]))


def _is_line_window(chunk: FileChunk) -> bool:
    """A raw-source window from :func:`fnd.extract._text.line_windows`."""
    return len(chunk.blocks) == 1 and chunk.blocks[0].kind == "code" and chunk.line > 0


def _trimmed(
    chunk: FileChunk, repeat: int, *, before: FileChunk | None, before_repeat: int, lead: str
) -> tuple[FileChunk, str]:
    """``chunk`` marked with ``repeat``; a fenced window also drops it, seamed, and hands
    its trailing blank lines to the next fence, as a fence never shows its own."""
    spec = KIND_BY_ID.get(chunk.kind)
    code = chunk.body_text.rstrip("\n")
    if spec is None or unfenced(chunk.body_md) != (spec.fence_lang, code):
        return dataclasses.replace(chunk, repeat=repeat), ""
    own, handed = shown_text(chunk, repeat), ""
    if repeat:
        lines = own[:-1].split("\n")
        blank = len(lines) - len(list(dropwhile(lambda ln: not ln.strip(), reversed(lines))))
        blank = min(blank, len(lines) - 1)
        own = "\n".join(lines[: len(lines) - blank])
        handed = "".join(f"{ln}\n" for ln in lines[len(lines) - blank :])
    lead = lead if before is not None else ""
    windows = [(chunk.body_text, len(lead))]
    if before is not None:
        windows.append((before.body_text, before_repeat - len(before.body_text) + len(lead)))
    cut = Seam(code=True)
    body_md = with_seams(
        fenced(lead + own, spec.fence_lang),
        above=cut if before is not None else None,
        below=cut if repeat else None,
    )
    trimmed = dataclasses.replace(chunk, repeat=repeat, body_md=body_md, windows=tuple(windows))
    return trimmed, handed

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
from typing import TYPE_CHECKING, NamedTuple

from fnd.extract._fences import fenced, unfenced
from fnd.extract.plain import OVERLAP_CHARS
from fnd.kinds import KIND_BY_ID
from fnd.seams import Seam, with_seams

if TYPE_CHECKING:
    from fnd.matching import MatchSpec
    from fnd.query import FileChunk, FileGroup, Hit

__all__ = ["fold_repeated_sections", "mark_repeats", "shown_text"]


def mark_repeats(chunks: list[FileChunk]) -> list[FileChunk]:
    """``chunks`` (in ``chunk_seq`` order) with every verified window overlap marked."""
    repeats = [_repeated(_window(a), _window(b)) for a, b in pairwise(chunks)] + [0]
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


def fold_repeated_sections(
    groups: list[FileGroup], *, strict: MatchSpec, fuzzy: MatchSpec
) -> list[FileGroup]:
    """``groups`` with one section per match an overlap repeats: where every match of
    a section sits in the stretch the next listed one repeats, the preview lands both
    on it, so the lower-ranked of the two goes. A fuzzy-pass section is judged by
    ``fuzzy``, every other by ``strict``."""
    return [_folded(g, strict, fuzzy) for g in groups]


def _folded(group: FileGroup, strict: MatchSpec, fuzzy: MatchSpec) -> FileGroup:
    from fnd.query import FUZZY_PASS_INDEX, decoded_body
    from fnd.render import text_has_any_match

    by_seq = {h.chunk_seq: h for h in group.hits}
    texts: dict[int, str] = {}

    def window(hit: Hit) -> _Window:
        if hit.chunk_seq not in texts:
            texts[hit.chunk_seq] = decoded_body(hit)[0]
        return _Window(hit.kind, hit.line, texts[hit.chunk_seq], hit.body_md)

    drop: set[int] = set()
    for seq in sorted(by_seq):
        hit, after = by_seq[seq], by_seq.get(seq + 1)
        if after is None or seq in drop or not _may_repeat(hit, after):
            continue
        own = window(hit)
        n = _repeated(own, window(after))
        spec = fuzzy if hit.pass_index == FUZZY_PASS_INDEX else strict
        cut = len(own.text) - n
        repeats_a_match = n and text_has_any_match(own.text[cut:], spec)
        if repeats_a_match and not _holds_a_match(own.text[:cut], spec):
            drop.add(after.chunk_seq if hit.rank_key > after.rank_key else seq)
    if not drop:
        return group
    return dataclasses.replace(group, hits=[h for h in group.hits if h.chunk_seq not in drop])


# Characters per early look at a long text. Measured on a 60,000-line .py with a
# match every 7 rows: the fold went from 63 ms a search to 1.9 ms.
_PIECE_CHARS = 1_000


def _holds_a_match(text: str, spec: MatchSpec) -> bool:
    """Whether ``text`` holds a match: line-cut pieces first, so a hit returns early,
    then the whole text, which alone can see a phrase across a cut."""
    from fnd.render import text_has_any_match

    start = 0
    while start < len(text) - _PIECE_CHARS:
        end = text.find("\n", start + _PIECE_CHARS)
        if end < 0:
            break
        if text_has_any_match(text[start:end], spec):
            return True
        start = end + 1
    return text_has_any_match(text, spec)


def _may_repeat(prev: Hit, curr: Hit) -> bool:
    """Whether two hits can be overlapping windows at all, before any body is decoded."""
    if prev.kind != curr.kind:
        return False
    return prev.kind == "txt" or (prev.body_md.startswith("```") and curr.body_md.startswith("```"))


def shown_text(chunk: FileChunk, repeat: int | None = None) -> str:
    """``chunk``'s text less the stretch the next chunk shows."""
    text = chunk.body_text
    return text[: len(text) - (chunk.repeat if repeat is None else repeat)]


class _Window(NamedTuple):
    kind: str
    line: int
    text: str
    body_md: str


def _window(chunk: FileChunk) -> _Window:
    return _Window(chunk.kind, chunk.line, chunk.body_text, chunk.body_md)


def _repeated(prev: _Window, curr: _Window) -> int:
    """Characters at the end of ``prev`` that ``curr`` repeats, or 0. A blank txt
    head proves nothing: a skipped blank window leaves matching blank ends."""
    if prev.kind != curr.kind:
        return 0
    a, b = prev.text, curr.text
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


def _is_line_window(window: _Window) -> bool:
    """A raw-source window from :func:`fnd.extract._text.line_windows`, fenced whole."""
    spec = KIND_BY_ID.get(window.kind)
    return (
        window.line > 0
        and spec is not None
        and unfenced(window.body_md) == (spec.fence_lang, window.text.rstrip("\n"))
    )


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

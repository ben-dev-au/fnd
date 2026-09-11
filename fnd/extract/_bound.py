"""The chunk-size contract, enforced where every extractor's output passes.

A chunk is at most ``MAX_CHUNK_CHARS`` of body. Extractors may split earlier
to yield better pieces (exact line numbers, whole rows); this is the guarantee
behind them, so a new extractor cannot ship an oversized chunk because it never
had the option. Measured: one dump with a whole post per line became 527
chunks of 33,000 words, and every query scoped to it paid for every word.

Pieces are cut at block boundaries, so one never starts mid-paragraph; a lone
block over the budget is cut at whitespace. Pieces inherit page, line, heading
path and timestamps, so deep-links stay exact. ``chunk_seq`` is assigned here,
monotonic per file, since splitting invalidates any numbering an extractor did.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Iterator

from fnd.extract.base import MAX_CHUNK_CHARS, Block, Chunk

__all__ = ["bounded"]

#: How much of a block's text is used to locate it in the verbatim source.
_LOCATE_CHARS = 48


def bounded(chunks: Iterable[Chunk]) -> Iterator[Chunk]:
    """Yield ``chunks`` with every body under the budget and ``chunk_seq`` renumbered."""
    seq = 0
    for chunk in chunks:
        for piece in _pieces(chunk):
            piece.chunk_seq = seq
            seq += 1
            yield piece


def _pieces(chunk: Chunk) -> Iterator[Chunk]:
    if len(chunk.body) <= MAX_CHUNK_CHARS:
        yield chunk
        return
    blocks = chunk.body_struct or [Block(kind="p", text=chunk.body)]
    for run in _block_runs(blocks):
        yield dataclasses.replace(
            chunk,
            body="\n".join(b.text for b in run).strip(),
            body_struct=list(run),
            body_md=_slice_source(chunk.body_md, run),
        )


def _block_runs(blocks: list[Block]) -> Iterator[list[Block]]:
    held: list[Block] = []
    size = 0
    for block in blocks:
        if len(block.text) > MAX_CHUNK_CHARS:
            if held:
                yield held
                held, size = [], 0
            for text in _split_text(block.text):
                yield [Block(kind=block.kind, text=text)]
            continue
        if held and size + len(block.text) + 1 > MAX_CHUNK_CHARS:
            yield held
            held, size = [], 0
        held.append(block)
        size += len(block.text) + 1
    if held:
        yield held


def _split_text(text: str) -> Iterator[str]:
    """Cut at the last whitespace under the budget, so a term is never halved."""
    start = 0
    while start < len(text):
        end = start + MAX_CHUNK_CHARS
        if end >= len(text):
            yield text[start:]
            return
        cut = text.rfind(" ", start, end)
        if cut <= start:
            cut = end
        yield text[start:cut]
        start = cut


def _slice_source(source: str, run: list[Block]) -> str:
    """The verbatim source behind ``run``, or "" when it cannot be located.

    Block text is a rendering of the source (markers and emphasis stripped),
    so the first and last blocks are found by their opening characters. A
    miss yields "", and the preview then lays out ``body_struct``, the path
    every kind without a markdown renderer already takes.
    """
    if not source or not run:
        return ""
    head = run[0].text.strip()[:_LOCATE_CHARS]
    tail = run[-1].text.strip()[:_LOCATE_CHARS]
    if not head or not tail:
        return ""
    start = source.find(head)
    if start < 0:
        return ""
    last = source.find(tail, start)
    if last < 0:
        return ""
    end = source.find("\n", last + len(run[-1].text.strip()))
    return source[start:] if end < 0 else source[start:end]

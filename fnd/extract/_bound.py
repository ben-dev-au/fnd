"""The chunk-size contract, enforced where every extractor's output passes.

A chunk is at most ``MAX_CHUNK_CHARS`` of body, or ``MAX_TABLE_CHARS`` for a
table: ranking and proximity are scored per chunk, so a table stays whole while
it can. Extractors may split earlier to yield better pieces (exact line numbers,
whole rows); this is the guarantee behind them, so a new extractor cannot ship an
oversized chunk because it never had the option. Measured: one dump with a whole post per line became 527
chunks of 33,000 words, and every query scoped to it paid for every word.

Pieces are cut at block boundaries, so one never starts mid-paragraph; a lone
block over the budget is cut at whitespace. Pieces inherit page, line, heading
path and timestamps, so deep-links stay exact. ``chunk_seq`` is assigned here,
monotonic per file, since splitting invalidates any numbering an extractor did.

A piece's ``body_md`` is sliced by the source spans its extractor stamped on
the blocks, or left empty. A wrong slice paints the wrong text; empty paints
the blocks.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable, Iterator
from itertools import takewhile

from fnd.extract.base import MAX_CHUNK_CHARS, MAX_TABLE_CHARS, Block, Chunk
from fnd.seams import Seam, with_seams
from fnd.text_canon import canonical

__all__ = ["bounded"]


def bounded(chunks: Iterable[Chunk]) -> Iterator[Chunk]:
    """Yield ``chunks`` with every body under the budget, every text field
    :func:`~fnd.text_canon.canonical`, ``chunk_seq`` renumbered, and each seam
    through a container or fence marked (see :mod:`fnd.seams`)."""
    seq = 0
    held: Chunk | None = None
    held_from: Chunk | None = None
    for chunk in chunks:
        for raw in _pieces(chunk):
            # After the cut: an extractor's spans count lines its own way.
            piece = _canonical(raw)
            piece.chunk_seq = seq
            seq += 1
            if held is not None:
                _stitch(held, piece, same_chunk=held_from is chunk)
                yield held
            held, held_from = piece, chunk
    if held is not None:
        yield held


def _stitch(above: Chunk, below: Chunk, *, same_chunk: bool) -> None:
    """Mark the seam between consecutive pieces when a container or fence crosses it."""
    if not (above.body_struct and below.body_struct):
        return
    last, first = above.body_struct[-1], below.body_struct[0]
    pairs = zip(last.containers, first.containers, strict=False)
    crossing = [a for a, _ in takewhile(lambda pair: pair[0] == pair[1], pairs)]
    # A fence's pieces share its opening line as head, and never leave its chunk.
    code = (
        same_chunk
        and first.kind == last.kind == "code"
        and first.head is not None
        and first.head == last.head
    )
    if not crossing and not code:
        return
    seam = Seam(levels=tuple(c.level for c in crossing), code=code)
    # Each side on its own: a piece after plain lines still reopens its items.
    if above.body_md:
        above.body_md = with_seams(above.body_md, below=seam)
    if below.body_md:
        below.body_md = with_seams(below.body_md, above=seam)


_TEXT_FIELDS = ("body", "body_md", "heading_path", "title", "author", "page_label")


def _canonical(chunk: Chunk) -> Chunk:
    """``chunk`` itself when already canonical, the common case, else a copy that is."""
    texts = {name: canonical(getattr(chunk, name)) for name in _TEXT_FIELDS}
    blocks = [
        b if canonical(b.text) == b.text else dataclasses.replace(b, text=canonical(b.text))
        for b in chunk.body_struct
    ]
    same = all(texts[n] == getattr(chunk, n) for n in _TEXT_FIELDS)
    if same and all(a is b for a, b in zip(blocks, chunk.body_struct, strict=True)):
        return chunk
    return dataclasses.replace(chunk, body_struct=blocks, **texts)


def _pieces(chunk: Chunk) -> Iterator[Chunk]:
    offset = chunk.line - 1
    if len(chunk.body) <= MAX_CHUNK_CHARS:
        if chunk.body_md and chunk.body_struct:
            chunk.body_md = _reopened(chunk.body_md, chunk.body_struct[0], offset)
        yield chunk
        return
    blocks = chunk.body_struct or [Block(kind="p", text=chunk.body)]
    for run in _block_runs(blocks):
        body_md = _slice_source(chunk.body_md, run)
        if body_md:
            top = run[0].head or run[0].span
            assert top is not None
            body_md = _reopened(body_md, run[0], offset + top[0])
        yield dataclasses.replace(
            chunk,
            body="\n".join(b.text for b in run).strip(),
            body_struct=list(run),
            body_md=body_md,
        )


def _reopened(body_md: str, first: Block, top: int) -> str:
    """``body_md``, starting on absolute line ``top``, under the items it starts inside."""
    openers = [
        c.opener for c in first.containers if c.kind == "item" and c.start < top and c.opener
    ]
    return "\n".join([*openers, body_md]) if openers else body_md


def _block_runs(blocks: list[Block]) -> Iterator[list[Block]]:
    held: list[Block] = []
    size = 0
    for block in blocks:
        if block.kind == "table" and len(block.text) <= MAX_TABLE_CHARS:
            # Its own run, never split nor merged: a table is one unit.
            if held:
                yield held
                held, size = [], 0
            yield [block]
            continue
        if len(block.text) > MAX_CHUNK_CHARS:
            if held:
                yield held
                held, size = [], 0
            for text in _split_text(block.text):
                yield [dataclasses.replace(block, text=text, span=None, head=None)]
            continue
        if held and size + len(block.text) + 1 > MAX_CHUNK_CHARS:
            yield held
            held, size = [], 0
        held.append(block)
        size += len(block.text) + 1
    if held:
        yield held


_LAST_SPACE = re.compile(r"\s(?=\S*$)")


def _split_text(text: str) -> Iterator[str]:
    """Cut at the last whitespace under the budget, so a term is never halved.

    Any whitespace: a block with newlines or tabs and no spaces otherwise fell
    to the hard cut and halved an identifier.
    """
    start = 0
    while start < len(text):
        end = start + MAX_CHUNK_CHARS
        if end >= len(text):
            yield text[start:]
            return
        last = _LAST_SPACE.search(text, start, end)
        cut = last.start() if last and last.start() > start else end
        yield text[start:cut]
        start = cut


def _slice_source(source: str, run: list[Block]) -> str:
    """The verbatim source behind ``run``, by the spans its extractor stamped.

    Exact or empty, never a guess: a run holding any block without a span
    (a split single block, or a kind whose extractor has no source map) yields
    "", and the preview lays out ``body_struct`` instead, the path every kind
    without a markdown renderer already takes.
    """
    if not source or not run:
        return ""
    spans = [b.span for b in run]
    if any(s is None for s in spans):
        return ""
    first, last = spans[0], spans[-1]
    assert first is not None
    assert last is not None
    lines = source.splitlines()
    body = lines[max(first[0], 0) : last[1]]
    # Any run length: a short last piece shares a run with the blocks after it.
    head = run[0].head
    if head is not None:
        return "\n".join(lines[max(head[0], 0) : head[1]] + body)
    return "\n".join(body)

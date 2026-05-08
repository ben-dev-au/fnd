"""DOCX extractor: one chunk per heading section.

Walks paragraphs in document order; accumulates body until a Heading 1/2/3
paragraph appears, at which point it flushes the current section as a chunk
and starts a new one. Tracks an ancestor stack so each chunk ships its full
``heading_path`` like ``Methods Document > Sampling``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

from docx import Document
from docx.text.paragraph import Paragraph

from acorn.extract.base import Block, Chunk

# Map a paragraph style name to a heading level. Anything else = body.
_HEADING_LEVELS: dict[str, int] = {
    "Heading 1": 1,
    "Heading 2": 2,
    "Heading 3": 3,
    "Heading 4": 4,
    "Heading 5": 5,
    "Heading 6": 6,
    "Title": 1,
}


def _parent_id(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()


def _heading_level(p: Paragraph) -> int:
    style = p.style
    name = getattr(style, "name", "") if style is not None else ""
    return _HEADING_LEVELS.get(name, 0)


def _flush(
    *,
    path: Path,
    parent_id: str,
    mtime: int,
    heading_stack: list[str],
    blocks: list[Block],
    body_parts: list[str],
    seq: int,
    deck_title: str,
) -> Chunk | None:
    body = "\n".join(b for b in body_parts if b).strip()
    if not body and not heading_stack:
        return None
    heading_path = " > ".join(heading_stack)
    text_for_body = (
        (heading_path + "\n" + body) if heading_path and body else (heading_path or body)
    )
    return Chunk(
        parent_id=parent_id,
        path=str(path),
        mtime=mtime,
        kind="docx",
        body=text_for_body,
        body_struct=blocks.copy(),
        heading_path=heading_path,
        title=deck_title,
        chunk_seq=seq,
    )


def extract(path: Path) -> Iterator[Chunk]:
    parent_id = _parent_id(path)
    mtime = int(path.stat().st_mtime)
    doc = Document(str(path))

    heading_stack: list[str] = []
    blocks: list[Block] = []
    body_parts: list[str] = []
    seq = 0
    doc_title = ""

    for para in doc.paragraphs:
        text = (para.text or "").strip()
        if not text:
            continue
        level = _heading_level(para)

        if level > 0:
            # Flush the section we were accumulating.
            chunk = _flush(
                path=path,
                parent_id=parent_id,
                mtime=mtime,
                heading_stack=heading_stack,
                blocks=blocks,
                body_parts=body_parts,
                seq=seq,
                deck_title=doc_title,
            )
            if chunk is not None:
                yield chunk
                seq += 1
            blocks = []
            body_parts = []

            # Push this heading onto the stack at its level.
            heading_stack[level - 1 :] = [text]
            if not doc_title and level == 1:
                doc_title = text
            blocks.append(Block(kind=f"h{level}", text=text))
        else:
            blocks.append(Block(kind="p", text=text))
            body_parts.append(text)

    # Final flush.
    chunk = _flush(
        path=path,
        parent_id=parent_id,
        mtime=mtime,
        heading_stack=heading_stack,
        blocks=blocks,
        body_parts=body_parts,
        seq=seq,
        deck_title=doc_title,
    )
    if chunk is not None:
        yield chunk

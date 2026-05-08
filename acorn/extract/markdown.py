"""Markdown extractor: one chunk per heading section.

Walks the markdown-it-py token stream, splits on headings, tracks an ancestor
stack so each section ships its full ``heading_path`` like ``Notes > Methods >
Sampling``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

from markdown_it import MarkdownIt

from acorn.extract.base import Block, Chunk

_md = MarkdownIt("commonmark")


def _parent_id(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()


def _flush_section(
    *,
    path: Path,
    parent_id: str,
    mtime: int,
    heading_stack: list[str],
    blocks: list[Block],
    body_text_parts: list[str],
    seq: int,
) -> Chunk | None:
    body = "\n".join(p for p in body_text_parts if p).strip()
    if not body:
        return None
    heading_path = " > ".join(heading_stack)
    return Chunk(
        parent_id=parent_id,
        path=str(path),
        mtime=mtime,
        kind="md",
        body=f"{heading_path}\n{body}" if heading_path else body,
        body_struct=blocks.copy(),
        heading_path=heading_path,
        title=heading_stack[0] if heading_stack else "",
        chunk_seq=seq,
    )


def extract(path: Path) -> Iterator[Chunk]:
    source = path.read_text(encoding="utf-8")
    if not source.strip():
        return

    parent_id = _parent_id(path)
    mtime = int(path.stat().st_mtime)
    tokens = _md.parse(source)

    heading_stack: list[str] = []  # current ancestor headings, indexed by level (1..6)
    blocks: list[Block] = []
    body_parts: list[str] = []
    seq = 0
    in_heading = False
    pending_heading_level = 0

    i = 0
    while i < len(tokens):
        tok = tokens[i]

        if tok.type == "heading_open":
            # Flush the section we were accumulating.
            chunk = _flush_section(
                path=path,
                parent_id=parent_id,
                mtime=mtime,
                heading_stack=heading_stack,
                blocks=blocks,
                body_text_parts=body_parts,
                seq=seq,
            )
            if chunk is not None:
                yield chunk
                seq += 1
            blocks = []
            body_parts = []

            in_heading = True
            pending_heading_level = int(tok.tag[1])  # h1 -> 1
            i += 1
            continue

        if tok.type == "heading_close":
            in_heading = False
            i += 1
            continue

        if in_heading and tok.type == "inline":
            text = tok.content.strip()
            # Truncate the heading stack to the level above and push.
            heading_stack[pending_heading_level - 1 :] = [text]
            blocks.append(Block(kind=f"h{pending_heading_level}", text=text))
            i += 1
            continue

        if tok.type == "inline":
            text = tok.content.strip()
            if text:
                blocks.append(Block(kind="p", text=text))
                body_parts.append(text)
            i += 1
            continue

        # Other token types (lists, fences, hr, etc.) — skip cleanly for v1; they
        # still contribute their inline children when those inlines arrive.
        i += 1

    # Final flush.
    chunk = _flush_section(
        path=path,
        parent_id=parent_id,
        mtime=mtime,
        heading_stack=heading_stack,
        blocks=blocks,
        body_text_parts=body_parts,
        seq=seq,
    )
    if chunk is not None:
        yield chunk

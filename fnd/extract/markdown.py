"""Markdown extractor: one chunk per heading section.

Walks the markdown-it-py token stream, splits on headings, tracks an ancestor
stack so each section ships its full ``heading_path`` like ``Notes > Methods >
Sampling``.

Each chunk also carries the verbatim markdown source for its section in
``body_md`` — the TUI's structural preview renderer (Textual Markdown
widget) consumes this. ``body_struct`` continues to carry a flat list of
plain-text Blocks for the snippet pipeline; the two are intentionally
separate so the snippet path doesn't end up showing markdown markers like
``**bold**`` or ``# heading`` to the user.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

from markdown_it import MarkdownIt

from fnd.extract.base import Block, Chunk, ExtractError

_md = MarkdownIt("commonmark")

# Token types that carry "real" content. A section is worth flushing as a
# chunk when *any* of these appeared inside it — even if no `inline`
# tokens did. Pre-fix the extractor silently dropped sections that
# contained only a fenced code block (``code`` kind, no inline children),
# losing them from the index entirely.
_CONTENT_TOKEN_TYPES: frozenset[str] = frozenset(
    {
        "inline",
        "fence",
        "code_block",
        "table_open",
        "bullet_list_open",
        "ordered_list_open",
        "blockquote_open",
        "hr",
    }
)


def _parent_id(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode("utf-8"), usedforsecurity=False).hexdigest()


def _flush_section(
    *,
    path: Path,
    parent_id: str,
    mtime: int,
    heading_stack: list[str],
    blocks: list[Block],
    body_text_parts: list[str],
    body_md: str,
    has_content: bool,
    seq: int,
    section_start_line: int,
) -> Chunk | None:
    if not has_content:
        return None
    body = "\n".join(p for p in body_text_parts if p).strip()
    heading_path = " > ".join(heading_stack)
    # F_BODY = chunk's own visible text (own heading + paragraphs).
    # Ancestor heading_path is searched via F_HEADING_PATH as a boost,
    # NOT baked into body — inheriting parent-heading text into every
    # descendant's body inflates scores and surfaces chunks whose
    # visible content has no match.
    return Chunk(
        parent_id=parent_id,
        path=str(path),
        mtime=mtime,
        kind="md",
        body=body,
        body_struct=blocks.copy(),
        body_md=body_md,
        heading_path=heading_path,
        title=heading_stack[0] if heading_stack else "",
        chunk_seq=seq,
        # `section_start_line` is 0-based (token.map index); deep-link
        # templates want 1-based.
        line=section_start_line + 1,
    )


def _section_source(source_lines: list[str], start_line: int, end_line: int) -> str:
    """Slice the raw source for a heading section.

    ``start_line`` is the line of the section's heading_open (inclusive),
    ``end_line`` is the line of the *next* heading_open (exclusive), or
    ``len(source_lines)`` for the final section. The slice is verbatim
    so tables, fenced code, lists, blockquotes — everything markdown-it
    parses — round-trips into the renderer untouched.
    """
    start = max(start_line, 0)
    end = max(min(end_line, len(source_lines)), start)
    return "\n".join(source_lines[start:end]).rstrip()


def extract(path: Path) -> Iterator[Chunk]:
    try:
        yield from _extract_inner(path)
    except ExtractError:
        raise
    except Exception as e:
        raise ExtractError(str(path), f"{type(e).__name__}: {e}") from e


def _extract_inner(path: Path) -> Iterator[Chunk]:
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise ExtractError(str(path), f"not valid utf-8: {e}") from e
    if not source.strip():
        return

    parent_id = _parent_id(path)
    mtime = int(path.stat().st_mtime)
    tokens = _md.parse(source)
    source_lines = source.splitlines()
    total_lines = len(source_lines)

    heading_stack: list[str] = []  # current ancestor headings, indexed by level (1..6)
    blocks: list[Block] = []
    body_parts: list[str] = []
    seq = 0
    in_heading = False
    pending_heading_level = 0
    section_start_line = 0  # source line where the current section begins
    section_has_content = False  # any non-trivial token seen since last flush

    i = 0
    while i < len(tokens):
        tok = tokens[i]

        if tok.type == "heading_open":
            # Source line of the new heading — defines the boundary
            # for the *previous* section's slice.
            heading_line = tok.map[0] if tok.map else section_start_line
            # Flush the section we were accumulating, slicing source up
            # to (but not including) this heading.
            chunk = _flush_section(
                path=path,
                parent_id=parent_id,
                mtime=mtime,
                heading_stack=heading_stack,
                blocks=blocks,
                body_text_parts=body_parts,
                body_md=_section_source(source_lines, section_start_line, heading_line),
                has_content=section_has_content,
                seq=seq,
                section_start_line=section_start_line,
            )
            if chunk is not None:
                yield chunk
                seq += 1
            blocks = []
            body_parts = []
            section_start_line = heading_line
            section_has_content = True  # the new heading itself counts as content

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
            # Index the chunk's own heading as part of its searchable
            # body so a query that matches the heading text returns this
            # chunk. Ancestor headings live in heading_path (separately
            # boosted); only this chunk's own heading goes into F_BODY.
            body_parts.append(text)
            i += 1
            continue

        if tok.type == "inline":
            text = tok.content.strip()
            if text:
                blocks.append(Block(kind="p", text=text))
                body_parts.append(text)
                section_has_content = True
            i += 1
            continue

        # Fenced / indented code carries its text on ``tok.content`` with
        # no ``inline`` children, so unlike paragraphs it would never reach
        # F_BODY via the inline branch above. Index it so code-only matches
        # are findable, and emit a Block so snippets can surface the line.
        if tok.type in ("fence", "code_block"):
            code = tok.content.strip()
            if code:
                blocks.append(Block(kind="code", text=code))
                body_parts.append(code)
            section_has_content = True
            i += 1
            continue

        # Other token types (lists, hr, tables, blockquotes) — we don't
        # expand them into the legacy Block list (the inline children that
        # DO appear already cover the searchable text), but flag the section
        # as having content so a table-only section flushes as its own chunk.
        if tok.type in _CONTENT_TOKEN_TYPES:
            section_has_content = True
        i += 1

    # Final flush — slice to end of file.
    chunk = _flush_section(
        path=path,
        parent_id=parent_id,
        mtime=mtime,
        heading_stack=heading_stack,
        blocks=blocks,
        body_text_parts=body_parts,
        body_md=_section_source(source_lines, section_start_line, total_lines),
        has_content=section_has_content,
        seq=seq,
        section_start_line=section_start_line,
    )
    if chunk is not None:
        yield chunk
